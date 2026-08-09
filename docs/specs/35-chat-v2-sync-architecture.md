# Spec 35 — Chat v2 Sync Architecture

## Status: Approved for Phase 2 implementation

## Purpose

This spec defines the replacement architecture for Cognis chat timeline state
across web chat, PWA, and future native mobile clients.

The goal is not to patch the current chat state machine. The goal is to replace
the active chat path with a small, explicit, reliable sync contract that can be
implemented, tested, reviewed, and trusted.

The current implementation already has the most important primitive: a canonical
append-only session event log. Today that backend is Intaris. Chat v2 must use
that source of truth, but it must not expose Intaris as a permanent public client
contract. Intaris is the current `SessionEventStore` implementation; future
session event-store backends must be possible without changing web/PWA/native
client semantics.

---

## Design precedents

Chat v2 intentionally borrows proven patterns instead of inventing a bespoke
chat protocol.

| Cognis Chat v2 concept | Precedent | What we copy | What we do not copy |
|---|---|---|---|
| Snapshot + incremental sync | Matrix `/sync` | Opaque cursor, initial snapshot, incremental changes, explicit gaps/reset | Full Matrix room/state complexity |
| Realtime cursor validation | Discord Gateway | Monotonic resume position and fail-fast recovery on invalid resume | Discord-specific session lifecycle |
| Idempotent sends | Slack/Mattermost-style client message IDs | Client transaction ID and server duplicate handling | Provider-specific timestamp message IDs |
| Local store + outbox | Stream/mobile chat SDKs | Network sync writes to local state; UI renders local materialized state | Heavy SDK abstraction or SaaS assumptions |
| Runtime overlay | Matrix ephemeral events, stricter | Separate volatile state from durable timeline | Silent merge of stale ephemeral state |

Principle:

```text
Realtime is an optimization. Sync/snapshot is the correctness mechanism.
```

---

## Non-negotiable invariants

1. **Canonical truth is append-only session events.**
   - Current backend: Intaris.
   - Public abstraction: `SessionEventStore`.
   - Cognis SQL remains metadata/state, not durable chat content.

2. **Client-visible final state is projection-backed.**
   - Final-looking messages, completed tool calls, completed thinking blocks,
     task cards, auth/question cards, and artifacts must be backed by source
     event references.
   - Runtime cache may show volatile progress, but it cannot make final truth.

3. **No silent gaps.**
   - Cursor mismatch, expired cursor, unknown projection version, or missing
     event range must force `/sync` recovery or `/snapshot`.
   - The frontend must never continue applying realtime frames after an
     unknown gap.

4. **No duplicate user-visible items.**
   - Deduplication has layers:
     - transport frame/cursor duplicate
     - timeline item ID duplicate
     - client transaction/message duplicate
     - source event duplicate

5. **Runtime overlay is replace-whole.**
   - No merge-preserve heuristics for active streams/spinners.
   - A newer runtime revision replaces the previous runtime overlay.
   - `has_active_turn=false` clears every volatile spinner/stream.

6. **The frontend does not infer global active-turn state from timeline quirks.**
   - Active turn/progress comes from `RuntimeOverlaySnapshot`.
   - Timeline item `status` can render item-local state only.

7. **The public API is store-backend-agnostic.**
   - Clients use opaque cursors and stable timeline item IDs.
   - Debug/audit source refs may mention `store: "intaris"`, but clients do
     not build sync logic from Intaris seqs.

8. **The implementation is modular.**
   - No new backend mega-route.
   - No new frontend mega-store/page.
   - Projector is pure and fixture-testable.
   - Runtime overlay is isolated.
   - Event-store adapter is isolated.

---

## Scope

### In scope

- New Chat v2 backend contract.
- New Chat v2 frontend store/sync engine.
- Typed timeline item union.
- Runtime overlay contract.
- Idempotent send contract.
- Golden projection fixtures.
- Expanded E2E coverage for timeline reliability.
- Cutover from old active chat path to v2.
- Decommission old active frontend timeline patch handling.

### Out of scope

- Replacing Intaris.
- Replacing the agent loop, turn scheduler, workflow engine, or tool execution
  architecture.
- Rewriting visual components that can be adapted cleanly.
- Full offline-first native implementation in this phase. The contract must
  support native/offline, but native apps can be implemented later.
- Public federation protocol. This is a Cognis client sync contract.

---

## Current architecture summary

The current web chat path combines:

- Intaris session events as durable source.
- Cognis SQL conversation/session metadata.
- REST `/messages`, `/timeline`, `/view` projections.
- WebSocket `chat_v2_frame`, runtime snapshots, state snapshots, and generic
  lifecycle/notification events.
- Frontend `ChatV2Store` scoped reconciliation and pure sync-engine invariants.

Known problem classes:

- Session-local sequence numbers are projected into a conversation timeline with
  synthetic lineage/sentinel ordering.
- Public timeline item shape is loose and contract-critical fields are not
  schema-enforced.
- `legacy timeline event.last_seq` is not a durable transport sequence.
- Active turn state comes from multiple payloads and local inference.
- Runtime/live ordering can rely on process-local counters.
- Legacy WebSocket events and canonical patches can both mutate frontend state.
- Frontend compensates with preservation and dedupe heuristics, which makes
  correctness hard to reason about.

Chat v2 replaces the active client path with one contract:

```text
SessionEventStore events + Cognis metadata
  -> deterministic projection
  -> ChatSnapshot / ChatSyncResponse / ChatRealtimeFrame
  -> local client store
  -> UI selectors and presentational components
```

---

## Architecture overview

```text
                  ┌──────────────────────────┐
                  │ Cognis SQL metadata       │
                  │ conversation/session/todo │
                  └────────────┬─────────────┘
                               │
┌──────────────────────────┐   │
│ SessionEventStore         │   │
│ current: Intaris adapter  │   │
│ future: pluggable backend │   │
└────────────┬─────────────┘   │
             │                 │
             ▼                 ▼
      ┌─────────────────────────────┐
      │ Event normalizer             │
      │ backend event -> stable input│
      └────────────┬────────────────┘
                   │
                   ▼
      ┌─────────────────────────────┐
      │ Pure deterministic projector │
      │ normalized events -> view    │
      └────────────┬────────────────┘
                   │
                   ▼
      ┌─────────────────────────────┐
      │ Chat v2 REST sync contract   │
      │ snapshot/sync/backfill/send  │
      └────────────┬────────────────┘
                   │
                   ▼
      ┌─────────────────────────────┐
      │ Web/PWA/native local store   │
      │ cursor + timeline + outbox   │
      └────────────┬────────────────┘
                   │
                   ▼
      ┌─────────────────────────────┐
      │ UI selectors/components      │
      └─────────────────────────────┘

Runtime scheduler/session cache
      │
      ▼
RuntimeOverlaySnapshot
      │
      ▼
replace-whole volatile UI overlay
```

### Multi-controller Redis acceleration

PostgreSQL remains authoritative for durable turn orchestration, ownership
leases, and fencing. Intaris remains authoritative for canonical content.
Redis is optional and disposable; it cannot create final timeline truth or
participate in readiness.

When configured, all controllers use one versioned Pub/Sub channel for volatile
runtime envelopes. Expiring latest-envelope keys and tombstones support
reconnect hydration and explicit clearing. This is not Streams, consumer
groups, or a durable queue. Receivers validate schema/version, ownership epoch,
generation, and fence, then reapply local authorization before WebSocket fanout.
Late frames from a replaced owner are rejected.

The event cache stores Intaris pages in controller-local L1 only. It stores
watermarks and bounded `ChatSnapshot` projections in Redis. These caches use
the same authority, policy, and generation namespace. Snapshot keys contain
HMAC-derived identities, the projection version, ordered session generations,
and a Work overview fence.
Envelopes validate the authority, conversation digest, lineage, Work fence,
and cursor watermarks. Append generation changes make older projections
unreadable. A Work commit changes the overview fence and requests another
snapshot warm. Thus, a cache-only read misses after the commit until the new
overview is warm. A background warm does not publish while Work coverage is
less than an event watermark. It waits for the post-commit warm instead. A
cache-only read does not refresh Work during the request.

A short token-fenced Redis lock coalesces cold projection rebuilds across
controllers. Active and queued conversations warm without WebSocket
subscribers. Background Work and snapshot event reads share a controller-local
admission limit. Intaris read failures cause bounded exponential backoff and
one recovery probe. Foreground event writes and unrelated endpoint families do
not use this backoff.

The Intaris append listener performs only local, exception-isolated admissions.
It invalidates L1 first. Then it records the warm mapping before it admits the
generation dispatcher. Finally, it admits Work projection data to a bounded,
session-coalesced queue. An accepted item evicted from the payload queue moves
to a lightweight, session-coalesced repair-intent map. This map retains no
event payload and grows only with concurrently dirty sessions. Saturation
therefore drops payload data but does not reject accepted append work. Missing,
noncontiguous, replaced, oversized, evicted, or lost batches that reach a
persisted repair state recover through direct authoritative reads. If repair
persistence fails, the controller retains the payload-free intent and retries
with bounded exponential backoff and jitter. A bounded shutdown raises and
keeps the intent visible if it cannot drain safely. Authorized Work access
creates and prioritizes missing projection states. The worker does not scan
sessions or poll healthy projections. A process crash after Intaris commits an
append but before Cognis retains repair intent can leave an existing caught-up
projection behind. The next append or explicit repair converges it.
`Session.updated_at` is not a reliable event-head signal, so the read path does
not infer repair from it.

The snapshot operation reuses the event cache's one-hour sliding TTL,
compression, and value bounds. Event pages keep L1 sliding expiration without
creating Redis page values or Redis touch work. Existing Redis page values
expire naturally. There is no startup cleanup and no separate projection TTL.
A Redis error is a cache miss, never a stale-on-error canonical read. Runtime
envelopes can be dropped. Snapshot and sync remain the correctness mechanism.

Without Redis, owner-local clients retain runtime detail while remote clients
render PostgreSQL-backed active-turn state and recover canonical content from
Intaris, with increased Intaris read amplification.

---

## Backend module layout

New code lives behind a v2 boundary:

```text
cognis/api/chat_v2/
  __init__.py
  schemas.py          # strict Pydantic contract models
  event_store.py      # SessionEventStore protocol + Intaris adapter boundary
  cursors.py          # opaque cursor encode/decode/validation
  normalizer.py       # raw backend events -> normalized events
  projector.py        # pure projection into ChatView
  shared_snapshot_cache.py # shared generation-fenced snapshot operation
  runtime_overlay.py  # scheduler/session-cache volatile state
  sync.py             # snapshot/sync/backfill orchestration
  routes.py           # FastAPI route bindings only
  realtime.py         # v2 realtime frame construction/fanout adapter
```

Rules:

- `routes.py` must stay thin.
- `projector.py` must not access runtime cache or WebSocket state.
- `runtime_overlay.py` must not create canonical timeline state.
- `event_store.py` hides Intaris-specific details from the rest of Chat v2.
- `schemas.py` is the single backend source for public v2 models.
- Send/idempotency handling must go through a Chat v2 ingress boundary and the
  existing turn/session machinery; Chat v2 routes must not call Intaris directly.

---

## SessionEventStore abstraction

The v2 contract uses a pluggable event-store protocol. The first implementation
wraps existing Intaris-backed session event reads.

```python
class SessionEventStore(Protocol):
    async def read_session_events(
        self,
        *,
        session_id: str,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: Literal["forward", "backward"] = "forward",
    ) -> SessionEventPage: ...

    async def read_session_high_watermark(
        self,
        *,
        session_id: str,
    ) -> SessionWatermark: ...
```

This protocol is intentionally read-oriented for projection. Canonical writes are
not performed by the projector or sync layer. User input enters through a
separate Chat v2 ingress/idempotency boundary and then uses the existing
transport-agnostic turn/session machinery to create canonical session events.
That machinery must itself remain behind a session-event-store writer boundary
as pluggable backends are introduced.

The Intaris adapter permits a high-watermark read to return sequence `0` when a
new session exists but its event stream has not yet been initialized. The
adapter verifies session metadata under the same scoped runtime identity before
accepting the stream `404`; missing or inaccessible sessions still fail the
snapshot/sync request.

Internal page shape:

```python
class SessionEventPage(BaseModel):
    store_id: str                 # "intaris" for the initial adapter
    session_id: str
    events: list[RawSessionEvent]
    first_seq: int | None
    last_seq: int | None
    has_more_before: bool
    has_more_after: bool

class RawSessionEvent(BaseModel):
    store_id: str
    session_id: str
    seq: int
    event_id: str | None = None   # adapter should provide if backend has one
    type: str
    timestamp: datetime | None = None
    lane: str | None = None
    prompt_visibility: str | None = None
    data: dict[str, Any]
```

Requirements:

- The adapter must preserve exact backend event ordering.
- The adapter must not synthesize semantic UI state.
- If a future backend has different event IDs, the normalizer maps them into the
  same normalized form.
- Cursor validation may use backend watermarks internally, but frontend clients
  receive only opaque cursors.
- Event lane and prompt-visibility metadata must be preserved when the backend
  exposes it. Missing metadata is normalized to the current main-lane behavior,
  not guessed from event content.

### Write and idempotency boundary

Chat v2 send correctness needs durable idempotency independent of the event-store
backend.

Add a small Cognis-owned idempotency ledger for client-submitted chat messages:

```ts
ChatClientTransaction {
  conversation_id: string
  principal_id: string
  client_txn_id: string
  client_message_id: string
  request_hash: string
  status: "accepted" | "queued" | "duplicate" | "failed"
  queue_id?: string | null
  message_id?: string | null
  source_ref?: SourceRef | null
  created_at: string
  updated_at: string
}
```

Rules:

- `principal_id` is the stable authenticated Cognis principal. In the current
  Cognis auth model this is the user's email address.
- Unique key: `(conversation_id, principal_id, client_txn_id)`.
- A retry with the same transaction and identical `request_hash` returns the
  existing transaction result.
- A retry with the same transaction and different `request_hash` is rejected
  with `409 Conflict`.
- The send route records/locks the transaction before submitting to the turn
  scheduler.
- The send route never treats its response as final timeline truth. Final
  visible state still comes from projection over session events.
- The route must not call Intaris directly. It submits through the existing
  turn/session path and later reconciles `message_id`/`source_ref` when the
  canonical user-message event is visible.
- Future event-store implementations must provide the writer used by the
  turn/session path, but that is below the Chat v2 public API.

---

## Projection model

### Source references

Every canonical timeline item includes source references for audit, debugging,
and deterministic reconciliation.

```ts
SourceRef {
  store: string              // "intaris" initially
  session_id: string
  seq: number
  event_id?: string | null
  event_type: string
}
```

Client behavior:

- The client may show source refs only in debug/devtools or explicitly
  developer-facing diagnostics.
- The client must not derive sync cursor or ordering from source refs.
- Product UI must not make `store`, `session_id`, or `seq` a stable user-facing
  concept.

### Conversation lineage

The projector receives an ordered conversation session lineage:

```ts
ConversationLineage {
  conversation_id: string
  sessions: ConversationSessionRef[]
}

ConversationSessionRef {
  session_id: string
  role: "root" | "compacted" | "rebased" | "delegated" | "managed" | "task_step"
  parent_session_id?: string | null
  previous_session_id?: string | null
  status?: string | null
  completion_reason?: string | null
  started_at?: string | null
  completed_at?: string | null
}
```

Initial implementation may use the existing conversation active-session lineage
rules. The public contract only exposes projected timeline/state and opaque
cursors.

Canonical raw events sort by `(lineage ordinal, session sequence, event ID)`.
The persisted `previous_session_id` chain assigns oldest-first ordinals, so
every event in a newer lineage session sorts after every event in an older
session even when their timestamps overlap or are equal. Timestamps are not a
canonical ordering input.

Cold snapshot reads exploit that invariant without changing the result. The
server reads backward event pages from the newest lineage suffix in adaptive
concurrent batches of 1, 2, 4, then at most 8 sessions per round. It expands
toward older sessions only while the bounded latest window remains incomplete.
Each selected session retains the full per-session page limit and its own
authority-bound reader. Sparse histories can still require every session, but
no batch exceeds eight concurrent page reads. All-session high watermarks remain
mandatory for signed cursors, reset detection, projection cache keys, and
Redis-free multi-controller correctness; adaptive pruning applies only to event
page payloads.

Lineage reset rules:

- Cursor payload includes the ordered set of sessions used for the projection.
- `/sync` compares current lineage set and order with cursor lineage.
- If any required session is missing, inaccessible, compacted beyond cursor
  range, or appears in a different order, server returns `reset_required=true`.
- Reset reason must be specific when possible:
  - `lineage_changed`
  - `history_compacted`
  - `cursor_invalid`
  - `projection_version_changed`
- Server must not attempt partial ops across uncertain compaction, rebase,
  undo/redo, or missing-stream boundaries.

### Normalized events

The normalizer maps raw backend events into a smaller, stable internal union.

Examples:

```ts
NormalizedEvent =
  | UserMessageEvent
  | AssistantMessageEvent
  | ThinkingEvent
  | ToolCallStartedEvent
  | ToolCallOutputEvent
  | ToolCallCompletedEvent
  | DelegationEvent
  | ManagedConversationEvent
  | TaskEvent
  | QuestionSetEvent
  | AuthChallengeEvent
  | CredentialRequestEvent
  | TodoStateEvent
  | ArtifactEvent
  | FileDiffEvent
  | NoticeEvent
  | ErrorEvent
```

The normalizer is where legacy event shape compatibility belongs. The frontend
must never need legacy event-specific repair code.

Every normalized event also carries lane metadata:

```ts
NormalizedEventBase {
  source_ref: SourceRef
  lane: "main" | "side" | "system" | "tool" | "debug" | string
  prompt_visibility:
    | "main_context"
    | "side_context"
    | "hidden"
    | "audit_only"
    | string
}
```

Projection lane rules:

- The main chat timeline projects `lane="main"` user/assistant-facing events and
  explicitly supported user-visible operational events.
- Side-thread events are represented only by an explicit side-thread summary or
  linked card unless the user opens that side context.
- Audit-only, prompt-hidden, and internal/debug events must not silently appear
  as main timeline messages.
- Unknown lane/visibility combinations produce a debug counter and, only when
  user-visible safety requires it, a warning notice item. They must not leak
  hidden content into the main timeline.
- Golden fixtures must cover lane filtering and unsupported event visibility.

### Projector output

The projector returns:

```ts
ProjectedChatView {
  conversation: ConversationSummary
  timeline: TimelineWindow
  state: ConversationStateView
  queue: QueueState
  source_watermark: ProjectionWatermark
}
```

The projector must be:

- deterministic for identical inputs
- side-effect-free
- independent of runtime cache
- fixture-testable
- explicit about unsupported/unknown events via notice/error items or ignored
  debug counters, not silent UI corruption

---

## Public REST API

Chat v2 is a chat-contract version under the existing API version:

```text
/api/v1/chat/v2/...
```

A future global `/api/v2` can alias the same contract if needed.

### Common errors

All Chat v2 REST endpoints use the existing Cognis API error envelope where
possible, with these route-level semantics:

| HTTP status | Case | Client action |
|---|---|---|
| `400` | malformed request, invalid limit, invalid item cursor | fix request or show error |
| `401` | unauthenticated | re-authenticate |
| `403` | authenticated but no access to conversation | leave conversation view / show permission error |
| `404` | conversation or queue item not found | refresh conversation list or snapshot |
| `409` | idempotent send transaction reused with different request hash | mark outbox item failed; do not retry automatically |
| `410` | cursor expired or historical range unavailable | fetch snapshot |
| `422` | schema validation failure | client bug; log and show error |
| `426` | unsupported client/schema version | force app refresh/update |
| `500` | server failure | retry with backoff; do not mutate local canonical cursor |

Cursor-related failures should prefer a successful `ChatSyncResponse` with
`reset_required=true` when the client can recover normally. Use error responses
for malformed/tampered cursors, authorization failures, and unexpected server
failures.

### Snapshot

```http
GET /api/v1/chat/v2/conversations/{conversation_id}/snapshot
```

Purpose:

- initial load
- hard recovery after cursor mismatch
- projection-version mismatch recovery
- app resume when local state is missing or suspected corrupt

Response:

```ts
ChatSnapshot {
  schema_version: 2
  projection_version: string
  conversation: ConversationSummary
  timeline: TimelineWindow
  state: ConversationStateView
  queue: QueueState
  runtime: RuntimeOverlaySnapshot
  cursor: string
  server_time: string
}
```

Rules:

- Snapshot is authoritative for canonical state at `cursor`.
- Snapshot may include a window, not necessarily all historical items.
- `timeline.has_more_before` indicates older history is available.
- Runtime overlay is generated at response time and has its own revision.

### Sync

```http
GET /api/v1/chat/v2/conversations/{conversation_id}/sync?cursor={cursor}&limit=500
```

Limits:

- default: `500`
- minimum: `1`
- maximum: `1000`

Purpose:

- incremental foreground sync
- reconnect/resume recovery
- background/PWA/native app wake refresh

Response:

```ts
ChatSyncResponse {
  schema_version: 2
  projection_version: string
  conversation_id: string
  cursor_before: string
  cursor_after: string
  ops: ChatViewOp[]
  runtime?: RuntimeOverlaySnapshot | null
  reset_required: boolean
  reset_reason?: ChatResetReason | null
  has_more: boolean
  server_time: string
}
```

Client rules:

1. If `reset_required=true`, discard canonical local projection for this
   conversation and call `/snapshot`.
2. If local cursor equals `cursor_after`, treat the response as duplicate and
   ignore `ops`.
3. If local cursor does not equal `cursor_before`, do not apply `ops`; call
   `/sync` with the local cursor or `/snapshot` if the server rejects it.
4. If local cursor equals `cursor_before`, apply ops in order, then set local
   cursor to `cursor_after`.
5. Runtime overlay is applied independently by runtime epoch/revision.
6. If `has_more=true`, client immediately continues `/sync` from
   `cursor_after` before declaring the conversation fully idle.

### Cluster invalidation

In HA mode, PostgreSQL `LISTEN`/`NOTIFY` may wake clients connected to another
controller by carrying a bounded, allowlisted `scope_invalidated` pointer. The
signal contains scope IDs and a revision only; it never transports timeline
events or user content. The client preserves its signed cursor and calls
canonical `/sync`, falling back to `/snapshot` on reset. Signals are
best-effort: controller-level reconciliation periodically compares subscribed
scope watermarks, so lost notifications and listener reconnects cannot affect
correctness. Simple SQLite mode remains process-local.

### Backfill older timeline

```http
GET /api/v1/chat/v2/conversations/{conversation_id}/timeline?before={item_or_cursor}&limit=100
```

Limits:

- default: `100`
- minimum: `1`
- maximum: `200`

Purpose:

- scrollback pagination
- native local store history expansion

Response:

```ts
TimelineBackfillResponse {
  schema_version: 2
  projection_version: string
  conversation_id: string
  items: TimelineItem[]
  has_more_before: boolean
  before_cursor?: string | null
  server_time: string
}
```

Backfill rules:

- Backfill inserts older canonical items only.
- Backfill never carries runtime overlay.
- If projection version changed, server returns reset-style error and client
  reloads snapshot.

### Idempotent send

```http
PUT /api/v1/chat/v2/conversations/{conversation_id}/messages/{client_txn_id}
```

Request:

```ts
SendMessageV2Request {
  client_message_id: string
  content: string
  attachments: AttachmentRef[]
  chat_mode?: "default" | "plan" | "build" | null
}
```

Response:

```ts
SendMessageV2Response {
  status: "accepted" | "queued" | "duplicate"
  client_txn_id: string
  client_message_id: string
  conversation_id: string
  message_id?: string | null
  queue_id?: string | null
  cursor?: string | null
  server_time: string
}
```

Rules:

- `client_txn_id` is generated once by the client and persisted in the outbox.
- Retrying the same `client_txn_id` must not create a duplicate user message.
- If the original send was accepted, retry returns `duplicate` or the original
  accepted status with the same authoritative IDs.
- If the original send was queued, retry returns the same `queue_id`.
- `cursor` is only a recovery/sync hint. The frontend must not advance canonical
  local cursor from the send response alone.
- The authoritative user message eventually appears through snapshot/sync/
  realtime, not by trusting the send response as final timeline state.

### Queue delete

```http
DELETE /api/v1/chat/v2/conversations/{conversation_id}/queue/{queue_id}
```

Response:

```ts
QueueMutationResponse {
  conversation_id: string
  queue: QueueState
  cursor?: string | null
  runtime?: RuntimeOverlaySnapshot | null
  server_time: string
}
```

### Cancel active turn

```http
POST /api/v1/chat/v2/conversations/{conversation_id}/cancel
```

Response:

```ts
CancelTurnV2Response {
  conversation_id: string
  accepted: boolean
  reason?: string | null
  runtime?: RuntimeOverlaySnapshot | null
  server_time: string
}
```

### Tool output recovery

Tool output recovery may initially reuse the existing full-output route if the
payload is already stable. Chat v2 still owns the metadata contract exposed on
`ToolCallTimelineItem`.

If a v2 namespace is added, use:

```http
GET /api/v1/chat/v2/conversations/{conversation_id}/tool-output/{recovery_call_id}
```

Rules:

- Tool timeline item must include enough metadata for recovery:
  `has_full_output`, `recovery_call_id`, `tool_output_artifact_id`, `output_size`,
  and `truncated`.
- The recovery payload is not part of the timeline sync cursor.
- Recovery route authorization is conversation-scoped.
- Full output may live in the existing external tool-output/artifact store.

---

## Realtime API

Existing WebSocket transport may remain, but Chat v2 clients consume only
`chat_v2_frame` for canonical timeline/state changes.

```ts
ChatRealtimeFrame {
  type: "chat_v2_frame"
  schema_version: 2
  projection_version: string
  conversation_id: string
  cursor_before: string
  cursor_after: string
  ops: ChatViewOp[]
  runtime?: RuntimeOverlaySnapshot | null
  server_time: string
}
```

Client rules:

1. If `cursor_before === local.cursor`, apply ops and advance to
   `cursor_after`.
2. If `cursor_after === local.cursor`, treat as duplicate.
3. Otherwise mark the conversation as `syncing`, stop applying realtime frames
   for that conversation, and call `/sync` or `/snapshot`.
4. Runtime overlay is checked by epoch/revision and may still be applied if
   newer, but it must not advance canonical cursor.

Generic lifecycle events such as `message_complete` and `conversation_updated` may
continue for notification/sidebar consumers; renderable timeline state is canonical ChatV2.
migration. Chat v2 frontend must not use them to mutate timeline state.

---

## Cursor contract

Cursor is opaque to clients.

Properties:

- bound to `conversation_id`
- bound to `projection_version`
- encodes enough internal source watermarks to perform sync
- signed or server-verifiable to reject tampering
- may expire
- includes the projected session lineage order

Recommended internal cursor payload:

```ts
InternalChatCursorPayload {
  version: 1
  conversation_id: string
  projection_version: string
  session_watermarks: Array<{
    store: string
    session_id: string
    last_seq: number
  }>
  lineage: Array<{
    store: string
    session_id: string
    role: string
    ordinal: number
  }>
  view_revision: number
  issued_at: string
  expires_at?: string | null
}
```

Encoding:

- base64url JSON payload + HMAC signature, or
- server-side token ID backed by Redis/DB.

Initial implementation should prefer stateless signed cursors unless payload
size becomes an issue. Cursor contents are not secret, but they must be
tamper-evident.

`/sync` limit semantics:

- If all changes since cursor fit within `limit`, server returns ordered ops and
  advances `cursor_after`.
- If changes exceed `limit` but can be safely chunked, server may return a
  prefix of ops with `cursor_after` advanced to the last included source
  watermark and `has_more=true`.
- If safe chunking is not possible because of lineage changes, projection
  version changes, compaction/rebase, or missing source ranges, server returns
  `reset_required=true` with `range_too_large` or the more specific reset
  reason.
- Server must never skip intermediate canonical changes while still advancing
  the cursor.

Reset reasons:

```ts
ChatResetReason =
  | "cursor_invalid"
  | "cursor_expired"
  | "projection_version_changed"
  | "lineage_changed"
  | "history_compacted"
  | "range_too_large"
  | "server_restart_lost_runtime"
  | "unsupported_cursor"
```

---

## View operations

```ts
ChatViewOp =
  | UpsertTimelineItemOp
  | RemoveTimelineItemOp
  | ReplaceConversationOp
  | ReplaceStateOp
  | ReplaceQueueOp
  | ResetOp
```

```ts
UpsertTimelineItemOp {
  op: "upsert_item"
  item: TimelineItem
}

RemoveTimelineItemOp {
  op: "remove_item"
  id: string
  reason?: string | null
}

ReplaceConversationOp {
  op: "replace_conversation"
  conversation: ConversationSummary
}

ReplaceStateOp {
  op: "replace_state"
  state: ConversationStateView
}

ReplaceQueueOp {
  op: "replace_queue"
  queue: QueueState
}

ResetOp {
  op: "reset"
  reason: ChatResetReason
}
```

Initial sync implementation can be conservative:

- emit upserts for changed/new items
- emit replace queue/state when changed
- reset when lineage/projection complexity is uncertain

Do not implement clever partial diffs until the simple contract is proven.

---

## Timeline schema

### Common base

```ts
TimelineItemBase {
  id: string
  kind: TimelineItemKind
  sort_key: string
  source_refs: SourceRef[]
  created_at?: string | null
  updated_at?: string | null
  status?: TimelineItemStatus | null
  stable: boolean
}
```

`stable=false` is allowed only for runtime overlay items. Canonical timeline
items from snapshot/sync must be `stable=true`.

```ts
TimelineItemStatus =
  | "pending"
  | "running"
  | "waiting"
  | "complete"
  | "failed"
  | "cancelled"
```

```ts
TimelineItemKind =
  | "message"
  | "thinking"
  | "tool_call"
  | "delegation"
  | "managed_conversation"
  | "task"
  | "question_set"
  | "auth_challenge"
  | "credential_request"
  | "todo_state"
  | "artifact"
  | "file_diff"
  | "notice"
  | "error"
```

### Message item

```ts
MessageTimelineItem extends TimelineItemBase {
  kind: "message"
  role: "user" | "assistant" | "system"
  content: string
  message_id: string
  client_message_id?: string | null
  client_txn_id?: string | null
  turn_id?: string | null
  assistant_phase_index?: number | null
  attachments: AttachmentRef[]
  partial?: boolean
}
```

ID rules:

- user message: `message:user:{message_id}` when authoritative
- optimistic outbox item: `outbox:{client_txn_id}` until reconciled
- assistant message: `message:assistant:{message_id}:phase:{assistant_phase_index}`
- Runtime stream snapshots carry the scheduler-stamped
  `assistant_phase_index` as authoritative (`assistant_phase_authoritative:
  true`); phase inference is only a compatibility fallback for older or
  externally supplied snapshots.

### Thinking item

```ts
ThinkingTimelineItem extends TimelineItemBase {
  kind: "thinking"
  message_id?: string | null
  turn_id?: string | null
  assistant_phase_index?: number | null
  blocks: ThinkingBlock[]
  active_title?: string | null
}

ThinkingBlock {
  id: string
  title?: string | null
  content: string
  status?: "running" | "complete" | "failed" | null
}
```

Runtime thinking items may be volatile. Final thinking items must be canonical.
If a persisted provider thinking event lacks `block_id`, both canonical and
runtime projection fall back to `seq-{source_seq}` when source sequence metadata
is available. The normal live agent loop should synthesize stable block ids
before persistence so reload and runtime overlays merge by the same item id.

### Tool call item

```ts
ToolCallTimelineItem extends TimelineItemBase {
  kind: "tool_call"
  call_id: string
  tool_name: string
  display_name?: string | null
  turn_id?: string | null
  assistant_phase_index?: number | null
  arguments_preview?: string | null
  result_preview?: string | null
  streamed_output?: string | null
  is_error?: boolean
  duration_ms?: number | null
  attachments: AttachmentRef[]
  file_diffs: FileDiffRef[]
  output_size?: number | null
  truncated?: boolean
  has_full_output?: boolean
  recovery_call_id?: string | null
  tool_output_artifact_id?: string | null
}
```

Tool output contract:

- Small preview may be inline.
- Full output remains in external artifact/tool-output store.
- Metadata needed to recover full output must be persisted into canonical
  events/projection.
- Runtime streamed output is volatile and is replaced by canonical final item.

### Delegation item

```ts
DelegationTimelineItem extends TimelineItemBase {
  kind: "delegation"
  child_session_id: string
  agent_id?: string | null
  title?: string | null
  summary?: string | null
  status: TimelineItemStatus
  result_summary?: string | null
  result_anchors?: Record<string, string> | null
}
```

### Managed conversation item

```ts
ManagedConversationTimelineItem extends TimelineItemBase {
  kind: "managed_conversation"
  managed_conversation_id: string
  agent_id: string
  title?: string | null
  status: TimelineItemStatus
  result_summary?: string | null
}
```

### Task item

```ts
TaskTimelineItem extends TimelineItemBase {
  kind: "task"
  task_id: string
  title: string
  workflow_id?: string | null
  workflow_step?: string | null
  status: TimelineItemStatus
  result_summary?: string | null
  deliverable_ids: string[]
}
```

### Question/auth/credential items

```ts
QuestionSetTimelineItem extends TimelineItemBase {
  kind: "question_set"
  request_id: string
  title?: string | null
  questions: QuestionSpec[]
  status: "waiting" | "complete" | "cancelled"
}

AuthChallengeTimelineItem extends TimelineItemBase {
  kind: "auth_challenge"
  challenge_id: string
  challenge_kind: string
  label: string
  message: string
  metadata: Record<string, unknown>
  required_fields: string[]
  status: "waiting" | "complete" | "cancelled" | "failed"
}

CredentialRequestTimelineItem extends TimelineItemBase {
  kind: "credential_request"
  credential_request_id: string
  credential_id: string
  credential_kind: string
  label: string
  description?: string | null
  required_fields: string[]
  status: "waiting" | "complete" | "cancelled" | "failed"
}
```

### Artifact and file diff items

```ts
ArtifactTimelineItem extends TimelineItemBase {
  kind: "artifact"
  artifact_id: string
  filename: string
  mime_type?: string | null
  size_bytes?: number | null
  title?: string | null
}

FileDiffTimelineItem extends TimelineItemBase {
  kind: "file_diff"
  file_diffs: FileDiffRef[]
  title?: string | null
}
```

### Notice and error items

```ts
NoticeTimelineItem extends TimelineItemBase {
  kind: "notice"
  level: "info" | "warning"
  title: string
  message?: string | null
}

ErrorTimelineItem extends TimelineItemBase {
  kind: "error"
  level: "error"
  title: string
  message?: string | null
  error_code?: string | null
  recoverable: boolean
}
```

---

## Runtime overlay

Runtime overlay combines cluster-authoritative durable turn state with optional
owner-local scheduler/session-cache detail. PostgreSQL is authoritative for
whether a direct turn is active; process-local state only enriches an active
turn with live streams, thinking, tool output, and resolved chat mode.

```ts
RuntimeOverlaySnapshot {
  runtime_epoch: string
  runtime_revision: number
  generated_at: string
  has_active_turn: boolean
  active_turn?: RuntimeActiveTurn | null
  volatile_items: TimelineItem[]
}

RuntimeActiveTurn {
  turn_id: string
  session_id: string
  status: "starting" | "running" | "waiting" | "cancelling"
  chat_mode?: "default" | "plan" | "build" | null
  chat_mode_source?: string | null
  started_at?: string | null
  updated_at?: string | null
}
```

Runtime rules:

- Runtime identity is `(runtime_epoch, runtime_revision)`.
- `runtime_epoch` changes on controller/scheduler process restart or when a
  cluster-wide runtime state authority changes epoch.
- `runtime_revision` is monotonic per conversation within one epoch.
- Client applies runtime when:
  - local runtime is absent, or
  - `runtime_epoch` differs, or
  - `runtime_epoch` matches and `runtime_revision > local.runtime_revision`.
- If epoch changes, the server reconstructs `claimed`, `running`, and
  `absorbing` direct turns from `direct_turn_requests` before emitting runtime.
  This prevents a non-owner controller from clearing the spinner for an active
  turn owned by another replica.
- Applying runtime replaces previous volatile overlay completely.
- Runtime items must have `stable=false`.
- Canonical snapshot/sync items must have `stable=true`.
- Runtime overlay never advances canonical cursor.

Direct-turn transitions publish bounded `CHAT_SCOPE_CHANGED` invalidations and
conversation watermarks include durable-turn updates, so dropped notifications
are repaired by periodic reconciliation. Live token, thinking, and tool-output
frames remain owner-local volatile detail; they are not sent through PostgreSQL
notifications. A future shared runtime transport may add cross-controller live
frame replay without changing durable active-turn authority.

---

## Frontend architecture

New frontend modules:

```text
ui/src/lib/chat-v2/
  types.ts
  api.ts
  store.svelte.ts
  sync-engine.ts
  runtime-overlay.svelte.ts
  outbox.ts
  selectors.ts
```

Data flow:

```text
REST snapshot/sync + realtime frames
  -> sync engine
  -> local canonical store
  -> runtime overlay store
  -> selectors
  -> presentational components
```

Rules:

- UI components do not apply WebSocket events directly.
- Chat page does not own timeline merge logic.
- Chat page subscribes to selectors.
- Local store owns `cursor`, canonical timeline map, queue state, conversation
  state, runtime overlay epoch/revision, and outbox.
- Existing presentational components should be reused where possible.

### Local store shape

```ts
ChatV2ConversationStore {
  conversation_id: string
  cursor?: string | null
  projection_version?: string | null
  sync_status: "idle" | "syncing" | "gapped" | "error"
  timeline_by_id: Map<string, TimelineItem>
  timeline_order: string[]
  state?: ConversationStateView
  queue?: QueueState
  runtime?: RuntimeOverlaySnapshot
  outbox: OutboxEntry[]
  last_error?: string | null
}
```

### Apply algorithm

```ts
function applySnapshot(snapshot) {
  replace canonical conversation state
  replace timeline window
  replace queue/state
  set cursor = snapshot.cursor
  applyRuntime(snapshot.runtime)
  reconcileOutbox()
}

function applySyncResponse(response) {
  if (response.reset_required) return fetchSnapshot()
  if (cursor === response.cursor_after) return
  if (cursor !== response.cursor_before) return recoverFromGap()
  applyOps(response.ops)
  cursor = response.cursor_after
  applyRuntime(response.runtime)
  reconcileOutbox()
  if (response.has_more) continueSync(response.cursor_after)
}

function applyRealtimeFrame(frame) {
  if (cursor === frame.cursor_after) return
  if (cursor !== frame.cursor_before) return recoverFromGap()
  applyOps(frame.ops)
  cursor = frame.cursor_after
  applyRuntime(frame.runtime)
  reconcileOutbox()
}

function applyRuntime(runtime) {
  if (!runtime) return
  if (
    runtime.runtime_epoch === local.runtime_epoch &&
    runtime.runtime_revision <= local.runtime_revision
  ) return
  replace runtime overlay
}
```

### Outbox

Outbox entries are persisted before network send.

```ts
OutboxEntry {
  client_txn_id: string
  client_message_id: string
  conversation_id: string
  content: string
  attachments: AttachmentRef[]
  chat_mode?: "default" | "plan" | "build" | null
  status: "pending" | "sending" | "acked" | "failed"
  created_at: string
  updated_at: string
  last_error?: string | null
}
```

Initial web/PWA cutover requires durable IndexedDB-backed outbox persistence for
user sends. In-memory outbox is allowed only in isolated unit tests and story/dev
fixtures. If IndexedDB cannot ship in the cutover, offline/PWA send reliability
must be explicitly removed from the cutover acceptance criteria rather than
silently weakened.

---

## PWA and native mobile behavior

Foreground:

- Keep WebSocket connected when possible.
- Apply `chat_v2_frame` only if cursor matches.
- Recover through `/sync` or `/snapshot`.

Resume/wake:

- Load local store.
- Call `/sync?cursor=...`.
- If reset required, call `/snapshot`.

Offline send:

- Persist outbox entry first.
- Render optimistic pending item from outbox.
- Retry with same `client_txn_id`.
- Reconcile when canonical message arrives.

Push notification future:

- Push payload is only a wake hint.
- App must sync before trusting visible state.

Battery/network:

- No polling while realtime is healthy.
- Long-poll or periodic sync only when realtime unavailable.
- Bounded payloads and explicit reset for too-large deltas.

---

## Cutover strategy

This is a big-bang replacement of the active chat path, not incremental patching
of the old state machine.

However, implementation happens safely in parallel:

1. Add spec and review.
2. Add backend v2 modules/routes while old chat remains active.
3. Add golden fixtures and backend tests.
4. Add frontend v2 store and tests.
5. Adapt UI components to v2 props.
6. Switch chat page to v2 store once tests pass.
7. Remove old active frontend path.

Old backend routes may remain temporarily if other clients use them, but web chat
must stop consuming:

- legacy rendering events and loose projection items
- mutable `scoped ChatV2 store` merge-preserve heuristics
- multi-source active-turn inference

---

## Testing strategy

### Backend unit tests

```text
tests/unit/api/chat_v2/test_schemas.py
tests/unit/api/chat_v2/test_cursors.py
tests/unit/api/chat_v2/test_normalizer.py
tests/unit/api/chat_v2/test_projector_golden.py
tests/unit/api/chat_v2/test_runtime_overlay.py
tests/unit/api/chat_v2/test_sync.py
tests/unit/api/chat_v2/test_send_idempotency.py
```

Required assertions:

- schema rejects unknown invalid item shapes
- cursor tampering rejected
- cursor projection-version mismatch forces reset
- projector deterministic for identical fixtures
- no duplicate timeline IDs
- stable ordering across repeated projection
- source refs preserved
- runtime false clears volatile overlay
- stale runtime ignored by client tests
- send retry returns duplicate/same IDs

### Golden fixtures

Fixture directory:

```text
tests/unit/api/chat_v2/fixtures/
```

Scenario catalog:

- normal user/assistant turn
- streaming assistant turn
- thinking blocks
- tool running/completed/error
- streamed tool output
- delegation/sub-session
- managed conversation
- task card
- question set
- auth challenge
- credential request
- TODO updates
- queued message
- cancel active turn
- cancel queued message
- session rotation/compaction
- undo/redo/rebase
- reconnect mid-turn
- cache loss mid-turn
- lineage change/reset
- missing session stream
- unsupported/unknown event shape
- lane and prompt-visibility filtering
- idempotent resend with same transaction
- idempotent resend conflict with different payload
- stale runtime epoch/revision
- IndexedDB outbox recovery after reload

Each fixture contains raw normalized event input and expected `ChatSnapshot` or
projected timeline output.

### Frontend unit tests

```text
ui/src/lib/chat-v2/sync-engine.test.ts
ui/src/lib/chat-v2/store.test.ts
ui/src/lib/chat-v2/outbox.test.ts
ui/src/lib/chat-v2/selectors.test.ts
```

Required assertions:

- snapshot replaces canonical state
- matching frame applies ops
- duplicate frame ignored
- out-of-order frame triggers recovery
- missing frame triggers recovery
- runtime overlay replace-whole
- stale runtime ignored
- `has_active_turn=false` removes spinner/volatile streams
- optimistic outbox item reconciles with canonical item
- tool card does not duplicate between volatile and canonical state

### E2E expansion

Existing deterministic E2E harness must be extended rather than bypassed.

Add scenarios for:

- reload during active streaming
- reconnect after network drop
- duplicate realtime frame
- dropped realtime frame
- out-of-order realtime frame
- multi-tab same conversation
- send retry after timeout
- queued message cancel
- runtime cache loss
- PWA service-worker reload/update
- auth/question card answer flow
- tool output recovery flow
- undo/redo/rebase refresh

Acceptance:

- no stale spinner
- no duplicate timeline item
- no disappeared final assistant message
- no silent gap
- no lost queued/outbox message
- final visible state matches backend snapshot after recovery

---

## Code review gates

Every phase must pass review before the next phase.

| Phase | Deliverable | Review |
|---|---|---|
| 1 | This spec finalized | `system:architect` |
| 2 | backend schemas/event-store/cursor skeleton | code review |
| 3 | normalizer/projector/golden fixtures | code review |
| 4 | REST snapshot/sync/send routes | code review |
| 5 | realtime v2 frames | code review |
| 6 | frontend v2 store/sync/outbox | code review |
| 7 | UI cutover | code review |
| 8 | E2E expansion | code review |
| 9 | old path decommission | code review |

Phase cannot proceed with unresolved high/critical review findings.

---

## Implementation phases

### Phase 1 — Spec and review

- [ ] Add this spec.
- [ ] Update Obsidian progress tracker.
- [ ] Run independent architect review.
- [ ] Address review findings.
- [ ] Mark spec approved.

### Phase 2 — Backend skeleton

- [ ] Add `cognis/api/chat_v2` package.
- [ ] Add strict Pydantic schemas.
- [ ] Add `SessionEventStore` protocol.
- [ ] Add Intaris-backed event-store adapter boundary.
- [ ] Add cursor encode/decode/validation.
- [ ] Add schema/cursor unit tests.

### Phase 3 — Normalizer/projector

- [ ] Add normalized event model.
- [ ] Add normalizer for current Intaris/Cognis event shapes.
- [ ] Add pure projector.
- [ ] Add initial golden fixtures.
- [ ] Add projection determinism tests.

### Phase 4 — REST contract

- [ ] Add snapshot route.
- [ ] Add sync route.
- [ ] Add backfill route.
- [ ] Add idempotent send route.
- [ ] Add queue/cancel routes as needed.
- [ ] Add route tests.

### Phase 5 — Realtime v2

- [ ] Add `chat_v2_frame` construction.
- [ ] Add v2 subscription/reconnect handling.
- [ ] Add duplicate/out-of-order/gap tests.

### Phase 6 — Frontend v2 store

- [ ] Add TypeScript types.
- [ ] Add v2 API client.
- [ ] Add sync engine.
- [ ] Add local store.
- [ ] Add runtime overlay handling.
- [ ] Add outbox persistence interface.
- [ ] Add frontend unit tests.

### Phase 7 — UI cutover

- [ ] Adapt existing cards to v2 item props.
- [ ] Replace chat page data flow with v2 selectors.
- [ ] Remove old active timeline/spinner event handling.
- [ ] Ensure PWA reload path uses snapshot/sync.

### Phase 8 — E2E expansion

- [ ] Extend golden capture/replay for v2 frames.
- [ ] Add browser scenarios for reconnect/reload/offline cases.
- [ ] Validate against existing scenarios.

### Phase 9 — Decommission

- [x] Remove old active frontend timeline rendering usage.
- [x] Remove active old mutable timeline merge path from chat route.
- [ ] Remove old spinner state machine.
- [ ] Keep old backend routes only if still used outside v2 chat.

---

## Review decisions

Architect review status: **approved for implementation after minor clarifications**.

Changes incorporated after review:

- Runtime overlay uses `(runtime_epoch, runtime_revision)`, not a bare
  process-local integer.
- Send idempotency is backed by a Cognis-owned transaction ledger and does not
  call Intaris directly from Chat v2 routes.
- Cursor payload includes ordered lineage, and lineage changes force reset.
- Normalizer/projector contract includes lane and prompt-visibility handling.
- Web/PWA cutover requires durable IndexedDB-backed outbox for user sends.
- `/sync` limit semantics and `has_more` behavior are explicit.
- Common REST error semantics are specified.
- Tool-output recovery ownership is clarified.
- Golden fixture list includes lineage, missing stream, lane filtering,
  idempotent resend, stale runtime epoch, and IndexedDB outbox recovery cases.
- Send response cursor is explicitly only a recovery/sync hint.
- `/sync` and `/timeline` route limit defaults/min/max values are specified.
- Idempotency ledger uses the stable authenticated Cognis principal
  (`principal_id`, currently user email).

Non-blocking design choices to settle during implementation:

1. Whether Chat v2 remains under `/api/v1/chat/v2/...` permanently or receives
   a future `/api/v2/...` alias when the global API version changes.
2. Whether managed conversation child timelines are represented as summary cards
   only initially, or expandable inline details are implemented in the first UI
   cutover.
