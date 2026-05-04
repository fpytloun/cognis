# Cognis: Conversation Search

## Purpose

This spec defines first-class **conversation search** across Cognis: an
in-conversation find bar (Cmd+F), a global cross-conversation search in the
chat sidebar, and three LLM-facing tools (`list_conversations`,
`search_conversations`, `read_conversation_messages`) so agents can navigate
prior dialogue.

The bulk of the work is in **Intaris**: a new search subsystem with a
mandatory lexical index (Postgres FTS / SQLite FTS5) and an optional
embedding-backed semantic layer (pgvector or Qdrant). Cognis is a thin
proxy plus chat UI surfaces.

Related specs: [`05-integrations.md`](05-integrations.md),
[`06-tool-system.md`](06-tool-system.md), [`09-ui-ux.md`](09-ui-ux.md),
[`10-api-spec.md`](10-api-spec.md), [`13-nfr-operations.md`](13-nfr-operations.md),
[`28-agent-sharing.md`](28-agent-sharing.md).

## Motivation

Today there is no way for a user to find a past conversation by content.
The chat sidebar offers a client-side **title** filter over the loaded
conversation list and nothing more. Inside a long conversation there is
no Cmd+F equivalent — the user has to scroll. Long-running deployments
accumulate hundreds of sessions per user, each with potentially thousands
of events. The existing surface area does not scale.

Server-side support for full-text search must live in **Intaris**, not in
Cognis: Intaris is the authoritative store of conversation/session content
(events, intentions, summaries). Cognis only holds metadata (titles,
ownership, project bindings, status). Any cross-session text search has to
be answered by Intaris.

The constraint shaping this design: Cognis runs on **both SQLite and
Postgres** equally, single-user quickstart deployments must work without
extra services, and the UI must operate in many languages with
diacritic-tolerant matching. A purely lexical, language-aware index is
brittle on mixed-language sessions and short queries; a purely vector
index is heavy and SQLite-hostile. The chosen design layers a fast,
deterministic, language-agnostic lexical baseline under an optional
embedding layer that turns on automatically when configured.

## Design Principles

### 1. Intaris owns conversation content; Cognis owns metadata

Cognis search routes do not duplicate session content. They proxy to
Intaris, then **join** Intaris match rows (`session_id`, `event_seq`,
`snippet`) with Cognis `conversations` (title, agent_id, project_id,
ownership). The Cognis DB is never asked to full-text search event content.

### 2. Two-tier search inside Intaris

- **Tier 1 — Lexical** is mandatory and language-agnostic. Postgres uses
  `tsvector` with the `simple` config plus `pg_trgm` for substring/fuzzy
  fallback. SQLite uses an FTS5 contentless table with the `trigram`
  tokenizer and `remove_diacritics 2` to produce diacritic-folded matches
  in any Unicode-friendly language. No language detection, no per-language
  analyzers — the trigram/simple baseline is deterministic and works on
  Czech, German, Japanese, code identifiers, IDs, and partial words alike.
- **Tier 2 — Vector** is optional and improves recall on paraphrase and
  multilingual queries. Backends: `pgvector` (when Intaris runs on
  Postgres) or `qdrant` (URL-mode for shared deployments, local-mode path
  for single-user installs). Embeddings are produced through LiteLLM, so
  any LiteLLM-supported embedding provider works (OpenAI, Ollama, etc.).
  When the vector backend is unconfigured, unhealthy, or Tier 1 is forced,
  query degrades to lexical-only without losing functionality.

Hybrid scoring uses Reciprocal Rank Fusion (RRF) over Tier 1 + Tier 2
ranks. Pure lexical and pure vector modes are also selectable per query.

### 3. Feature-flagged, defaults to enabled

`INTARIS_SEARCH_ENABLED` is a master switch. When off, the search
endpoints return 404 and the indexer does not start. Cognis reads
`/api/v1/search/health` and hides search affordances (sidebar promote-to-
search, Cmd+F server fallback, LLM tools) accordingly. Default is `true`
for fresh installs; existing installs receive a backfill job on first
start.

### 4. Live indexing + outbox-backed backfill

Every event append, intention update, title update, and summary insert
emits an indexer job through a durable `search_outbox` table. The indexer
worker drains the outbox and writes to Tier 1 synchronously and to Tier 2
asynchronously (batched embedding calls). Restarting Intaris never loses
work — outbox rows are crash-safe.

Backfill is on-demand (`POST /api/v1/search/reindex`) plus auto-triggered
when the lexical schema version, vector backend, or embedding model
changes. Sessions are indexed in `last_activity_at DESC` order so recent
content becomes searchable first.

### 5. No tool-event indexing

Search indexes `user_message`, `assistant_message`, intention, title,
window summaries, compacted summaries, and agent summaries. Tool calls,
tool results, evaluations, delegations, and lifecycle events are **not**
indexed for search. Tool/audit data lives in the existing `audit_log` and
is queried directly through Intaris audit endpoints when needed; index
size and snippet quality are best when restricted to dialog content.

### 6. Strictly user-scoped, no admin bypass

All search routes (Intaris and Cognis) hard-scope by `user_id` derived
from the JWT. Admin role does not grant cross-user search visibility.
Shared agents (`agent_grants`) do not expand search scope: a grantee can
only find conversations they themselves own with that agent. This matches
the existing AGENTS.md rule "no admin bypass for user-owned resources".

### 7. Three LLM tools form the agent reading surface

`list_conversations` discovers; `search_conversations` finds; and
`read_conversation_messages` reads with rich pagination + anchors so the
agent can open at a specific match, page forward and backward across the
conversation lineage (root + compaction children + delegations), and
quote prior content reliably.

## Architecture

### Component overview

```
                    ┌──────────────────────────────────┐
                    │            Cognis UI             │
                    │                                  │
                    │  Chat header [🔍] ──┐            │
                    │                     ▼            │
                    │  ChatSearchBar (Cmd+F)           │
                    │     ├── client substring search  │
                    │     └── server fallback ─────────┼───┐
                    │                                  │   │
                    │  Sidebar search input (Enter)────┼───┤
                    │                                  │   │
                    └──────────────────────────────────┘   │
                                                           ▼
                    ┌──────────────────────────────────────────────────┐
                    │                Cognis controller                 │
                    │                                                  │
                    │  POST /api/v1/search/conversations               │
                    │  POST /api/v1/search/conversation/{id}           │
                    │  GET  /api/v1/search/health                      │
                    │                                                  │
                    │  cognis/api/routes/search.py                     │
                    │       │                                          │
                    │       │ (proxy + join with `conversations`)      │
                    │       ▼                                          │
                    │  IntarisProvider.search()                        │
                    │  IntarisProvider.search_sessions()               │
                    │  IntarisProvider.search_health()                 │
                    └────────────────┬─────────────────────────────────┘
                                     │ JWT (sub=user_email, aud=intaris)
                                     ▼
       ┌─────────────────────────────────────────────────────────────┐
       │                        Intaris                              │
       │                                                             │
       │  POST /api/v1/search           ←─ flat match list           │
       │  POST /api/v1/search/sessions  ←─ aggregated by session     │
       │  GET  /api/v1/search/health                                 │
       │  POST /api/v1/search/reindex                                │
       │  GET  /api/v1/search/reindex/{job_id}                       │
       │                                                             │
       │  intaris/intaris/search/                                    │
       │  ├── service.py           (query orchestrator)              │
       │  ├── indexer.py           (outbox worker)                   │
       │  ├── lexical/                                               │
       │  │   ├── postgres.py     (tsvector + pg_trgm)               │
       │  │   └── sqlite.py        (FTS5 trigram)                    │
       │  ├── vector/                                                │
       │  │   ├── pgvector.py                                        │
       │  │   ├── qdrant.py        (URL or local-mode path)          │
       │  │   └── disabled.py      (no-op)                           │
       │  ├── fusion.py            (RRF)                             │
       │  ├── snippet.py                                             │
       │  └── routes.py                                              │
       │                                                             │
       │  Tables:                                                    │
       │   • event_search_index    (lexical rows + tsvector/FTS5)    │
       │   • event_search_embeddings (pgvector only)                 │
       │   • search_outbox          (durable indexer queue)          │
       │   • search_state           (singleton: backend versions)    │
       └─────────────────────────────────────────────────────────────┘
```

### Indexed content

| Source | Trigger | `kind`            | Notes |
|--------|---------|-------------------|-------|
| Event append (type=`user_message`) | event store hook | `user_message`         | `text` = `event.data.content` |
| Event append (type=`assistant_message`) | event store hook | `assistant_message` | `text` = `event.data.content` |
| Session intention update | reasoning endpoint | `intention` | upsert `(session_id, "intention")` |
| Session title update | reasoning endpoint | `title` | upsert `(session_id, "title")` |
| `session_summaries` insert (`window`) | summary writer | `summary_window` | `text` = summary text |
| `session_summaries` insert (`compacted`) | summary writer | `summary_compacted` | |
| `agent_summaries` insert | summary writer | `agent_summary` | |

Tool calls, tool results, evaluations, delegations, lifecycle events,
checkpoints, and reasoning records are **not** indexed.

### Schema

#### `event_search_index`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigserial` (PG) / `integer` (SQLite) | Primary key |
| `session_id` | `text` | FK to `sessions(session_id)` |
| `user_id` | `text` | Denormalized for filter pushdown |
| `agent_id` | `text` | Denormalized |
| `event_seq` | `bigint` nullable | Null for `intention`/`title` upserts |
| `kind` | `text` enum | See table above |
| `role` | `text` nullable | `user`/`assistant`/`system` |
| `text` | `text` | Truncated to `INTARIS_SEARCH_MAX_TEXT_BYTES` |
| `text_normalized` | `text` nullable | Diacritic-folded fallback (used by `like` backend) |
| `lang` | `text` nullable | Reserved; not populated by current backends |
| `ts` | `timestamptz` | Event timestamp |
| `created_at` / `updated_at` | `timestamptz` | |

Postgres-only:

```sql
ALTER TABLE event_search_index
  ADD COLUMN tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', unaccent(coalesce(text, '')))) STORED;

CREATE INDEX ix_event_search_index_tsv ON event_search_index USING GIN (tsv);
CREATE INDEX ix_event_search_index_trgm ON event_search_index USING GIN (text gin_trgm_ops);
CREATE INDEX ix_event_search_index_user_ts ON event_search_index (user_id, ts DESC);
CREATE INDEX ix_event_search_index_session_seq ON event_search_index (session_id, event_seq);
```

If the `unaccent` extension is unavailable, the `tsv` column falls back to
`to_tsvector('simple', text)` and a Python-level `text_normalized` is used
for diacritic folding.

SQLite uses an FTS5 contentless shadow table:

```sql
CREATE VIRTUAL TABLE event_search_fts
USING fts5(
  text,
  content='event_search_index',
  content_rowid='id',
  tokenize='trigram remove_diacritics 2'
);

-- Triggers keep FTS5 in sync with event_search_index.
```

A `(session_id, kind)` partial unique constraint enforces upsert semantics
for `intention`/`title`/summary kinds.

#### `event_search_embeddings` (pgvector backend only)

```sql
CREATE TABLE event_search_embeddings (
  index_id    bigint PRIMARY KEY REFERENCES event_search_index(id) ON DELETE CASCADE,
  embedding   vector(1536) NOT NULL,
  model       text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_event_search_embeddings_hnsw
  ON event_search_embeddings USING hnsw (embedding vector_cosine_ops);
```

When the embedding dimension changes (model swap), the column type is
re-created and a backfill is auto-enqueued. Cognis-style bootstrap
handles this idempotently.

For Qdrant the equivalent payload lives in a Qdrant collection
(`INTARIS_SEARCH_QDRANT_COLLECTION`) keyed by `index_id` with payload
fields `user_id`, `agent_id`, `session_id`, `kind`, `ts`. Qdrant client
URL is single-setting: a normal `http(s)://...` URL for shared deployments,
or a `file:///path` (or absolute path) for local-mode in single-user
quickstart installs.

#### `search_outbox`

Durable indexer queue:

| Column | Type | Notes |
|--------|------|-------|
| `id` | bigserial | |
| `op` | text enum | `index_event`, `upsert_session_field`, `index_summary`, `delete_session`, `embed` |
| `payload` | jsonb / json text | Op-specific |
| `attempts` | int | |
| `next_attempt_at` | timestamptz | Backoff |
| `error` | text nullable | |
| `created_at` | timestamptz | |

The indexer drains rows in `(next_attempt_at ASC, id ASC)` order with
exponential backoff on failure.

#### `search_state` (singleton)

| Column | Notes |
|--------|-------|
| `lexical_schema_version` | bumped on lexical migration |
| `vector_backend` | last seen value (`disabled`/`pgvector`/`qdrant`) |
| `vector_model` | last embedding model |
| `vector_dim` | last embedding dimension |
| `backfill_job_id` nullable | |
| `backfill_status` | `idle`/`running`/`failed`/`done` |
| `backfill_total` / `backfill_processed` | |
| `updated_at` | |

When the running config diverges from the stored row at startup, a
backfill is auto-enqueued.

### Indexer pipeline

1. Event store append (or summary insert / intention update / title
   update) commits the source row plus a `search_outbox` row in the
   same transaction. This is the only synchronous write search adds to
   the hot path.
2. The indexer worker (one per Intaris process) polls `search_outbox`,
   batches embed jobs, and writes Tier 1 + (optionally) Tier 2.
3. Tier 1 writes are synchronous — failure marks the outbox row for
   retry but does not roll back the source append.
4. Tier 2 writes are asynchronous and deduped per `(index_id, model)`.
5. On `delete_session` (retention), the outbox carries a tombstone that
   purges all `event_search_index` rows for that session and removes
   matching points from Qdrant by filter or `event_search_embeddings`
   rows by FK cascade.
6. Per-provider circuit breaker on embedding calls (mirrors Cognis
   guardrails breaker family naming).
7. Metrics: outbox depth, batch latency p50/p95, embedding latency,
   retries, dropped jobs.

### Query pipeline

```
POST /api/v1/search
{
  "q": "rocket launch",
  "filters": {
    "agent_id?": "...",
    "session_id?": "...",
    "session_ids?": ["..."],
    "kind?": ["user_message", "assistant_message", "summary_compacted"],
    "from_ts?": "...", "to_ts?": "...",
    "alignment?": "...", "min_risk?": 0
  },
  "mode": "hybrid",      // "lexical" | "vector" | "hybrid" (default = best available)
  "limit": 50,
  "cursor": "..."
}
```

1. JWT decoded → `user_id` is forced into the filter set; never read from
   the body.
2. Resolve effective mode: requested mode ∩ available backends. Returns
   `X-Search-Degraded: vector_unavailable` if the request asked for vector
   but the backend is unhealthy.
3. Lexical query: BM25/`ts_rank` ranking on PG; `bm25` ranking on FTS5.
   For very short queries (< 3 normalized chars) or zero-hit lexical
   results, fall back to trigram similarity.
4. Vector query (when enabled): top-K embedding search with the same
   payload filters.
5. Fusion: RRF `score = Σ 1 / (k + rank_i)` with `k = 60`. Optional
   weighting via `INTARIS_SEARCH_HYBRID_ALPHA`.
6. Snippet: PG `ts_headline('simple', text, query, 'StartSel=<mark>,
   StopSel=</mark>,MaxFragments=2,MaxWords=20,MinWords=5')`; SQLite
   `snippet(event_search_fts, ...)`. For vector-only matches, snippet
   centers on the best-matching sentence.
7. Pagination cursor: opaque base64 of `{"score":..., "id":..., "rank":...}`
   so duplicate scores are tie-broken deterministically.

### Response shape

```jsonc
{
  "matches": [
    {
      "session_id": "ses_123",
      "event_seq": 42,
      "kind": "assistant_message",
      "role": "assistant",
      "ts": "2026-04-12T10:30:00Z",
      "snippet": "...the <mark>rocket</mark> launched at dawn...",
      "score": 0.83,
      "score_breakdown": {"lexical": 0.7, "vector": 0.92},
      "agent_id": "aria"
    }
  ],
  "next_cursor": "...",
  "total_estimated": 137,
  "backend": {
    "lexical": "postgres-fts",
    "vector": "pgvector",
    "mode_used": "hybrid"
  }
}
```

The aggregated variant (`POST /api/v1/search/sessions`) returns one row per
session with `top_match`, `match_count`, and a snippet from the highest-
scoring event. Used by the Cognis sidebar for the "Search results" group.

### Cognis proxy + join

`POST /api/v1/search/conversations` does the following per request:

1. Authenticate caller, derive `user_email`.
2. Forward to `Intaris.search_sessions(...)` (or `search()` when the
   caller asked for flat matches) with caller's structural filters
   (`agent_id`, `kind`, time range).
3. Join Intaris session matches with Cognis `sessions` (resolve
   `intaris_session_id → conversation_id`) and `conversations`
   (ownership, title, project_id, status).
4. Drop matches whose conversation is not owned by `user_email`. This is
   defense in depth — Intaris already scopes to `user_id`.
5. Apply Cognis-only filters (`project_id`, `status`).
6. Return `{matches: [...with conversation_id, conversation_title,
   agent_id, project_id, snippet, ...], next_cursor}`.

`POST /api/v1/search/conversation/{conversation_id}` is the in-conversation
server fallback. The handler walks the conversation's session lineage
(root + compaction children + sub-sessions) and forwards to Intaris with
`session_ids=[...]` filter, returning the flat match list.

`GET /api/v1/search/health` is a thin proxy with a 30s cache so the UI
can poll without hammering Intaris.

## LLM Tools

All three are router-handled (Pattern B), `read_only=True`,
`profile_group="research"`, and run through guardrails.

They live in `cognis/tools/builtin/conversations.py` and are wired from
`api/runtime_support.py::static_tool_definitions()` and
`_build_handler_map()`. Each handler reads `user_email` from the runtime
context and resolves the **current `conversation_id`** from
`runtime_metadata["conversation_id"]` (added by this stage; see below).

### `list_conversations`

```jsonc
{
  "name": "list_conversations",
  "description": "List your own conversations, filtered by agent, project, status, or time range.",
  "parameters": {
    "type": "object",
    "properties": {
      "agent_id":   {"type": "string"},
      "project_id": {"type": "string"},
      "status":     {"type": "string", "enum": ["active", "archived", "all"]},
      "since":      {"type": "string", "description": "ISO 8601 timestamp"},
      "until":      {"type": "string"},
      "limit":      {"type": "integer", "default": 25, "maximum": 100},
      "cursor":     {"type": "string"}
    }
  }
}
```

Returns `{conversations: [{conversation_id, title, agent_id, project_id,
status, last_message_at, message_count?}], next_cursor}`. Implementation
uses the existing `list_conversations` query helper.

### `search_conversations`

```jsonc
{
  "name": "search_conversations",
  "description": "Search across your conversations by content. Returns matches with snippets.",
  "parameters": {
    "type": "object",
    "properties": {
      "q":          {"type": "string"},
      "agent_id":   {"type": "string"},
      "project_id": {"type": "string"},
      "kinds":      {"type": "array", "items": {"type": "string"}},
      "from_ts":    {"type": "string"},
      "to_ts":      {"type": "string"},
      "mode":       {"type": "string", "enum": ["lexical", "vector", "hybrid"]},
      "limit":      {"type": "integer", "default": 20, "maximum": 50},
      "cursor":     {"type": "string"}
    },
    "required": ["q"]
  }
}
```

Returns `{matches: [{conversation_id, conversation_title, agent_id,
session_id, event_seq, kind, role, ts, snippet, score}], next_cursor,
backend}`. Returns `{"error": "search_disabled", ...}` when Intaris reports
search disabled — the LLM should treat this as a permanent capability
absence, not a transient failure.

### `read_conversation_messages`

```jsonc
{
  "name": "read_conversation_messages",
  "description": "Read user/assistant/summary events from a conversation, with anchor-based pagination across its session lineage.",
  "parameters": {
    "type": "object",
    "properties": {
      "conversation_id": {
        "type": "string",
        "description": "Defaults to the current conversation."
      },
      "anchor": {
        "type": "object",
        "oneOf": [
          {"required": ["kind"], "properties": {"kind": {"const": "latest"}}},
          {"required": ["kind", "session_id", "seq"],
           "properties": {"kind": {"const": "around"},
                           "session_id": {"type": "string"},
                           "seq": {"type": "integer"},
                           "before": {"type": "integer", "default": 5},
                           "after":  {"type": "integer", "default": 5}}},
          {"required": ["kind"], "properties": {"kind": {"const": "from_start"}}},
          {"required": ["kind", "session_id", "seq"],
           "properties": {"kind": {"const": "after"},
                           "session_id": {"type": "string"},
                           "seq": {"type": "integer"}}},
          {"required": ["kind", "session_id", "seq"],
           "properties": {"kind": {"const": "before"},
                           "session_id": {"type": "string"},
                           "seq": {"type": "integer"}}}
        ]
      },
      "cursor": {"type": "string"},
      "limit":  {"type": "integer", "default": 50, "maximum": 200},
      "kinds":  {"type": "array", "items": {"type": "string"}},
      "include_content_truncation": {"type": "boolean", "default": true}
    }
  }
}
```

Returns:

```jsonc
{
  "conversation_id": "conv_abc",
  "events": [
    {
      "session_id": "ses_123",
      "seq": 42,
      "kind": "assistant_message",
      "role": "assistant",
      "ts": "...",
      "content": "...",
      "content_truncated": false,
      "anchor": "ses_123:42"
    }
  ],
  "ordering": "chronological",
  "page": {
    "next_cursor": "...",
    "prev_cursor": "...",
    "anchor_used": {...},
    "total_estimated": 2371
  }
}
```

Mechanics:

- The handler resolves the lineage from the Cognis `sessions` table
  (root + chronological children) and orchestrates parallel
  `read_events` calls to Intaris with `after_seq`, `min_position`, and
  `max_position` filters, merging results by timestamp.
- `cursor` overrides `anchor`. Cursors are opaque base64 JSON
  `{"sid": "...", "seq": N, "dir": "f|b"}`.
- `next_cursor` is null when there are no more events forward;
  `prev_cursor` is null when there are no more events backward.
- `kinds` defaults to `["user_message", "assistant_message"]`. Tool/audit
  events are out of scope (use Intaris audit endpoints for those).
- `include_content_truncation=true` (default) truncates each event content
  to a fixed cap (e.g. 4 KB); when false the cap is the per-event hard
  ceiling (e.g. 32 KB) for cases where the agent really needs the full
  message.
- `total_estimated` may be `null` for very large lineages where exact
  counts are expensive — clients/agents must not assume it is precise.

Ownership: the handler verifies `conversations.user_email == caller_user_
email` before any Intaris call. **No admin bypass, no agent-grant
expansion.**

### Runtime metadata: `conversation_id`

Tool handlers gain access to the current conversation through
`runtime_metadata["conversation_id"]`. This is populated wherever
`RuntimeAccessContext` is constructed (turn scheduler, workflow engine,
channel inbound, executor WS) and falls through `runtime_metadata`
assembly in `cognis/api/runtime_support.py`. A new invariant test in
`tests/unit/test_api_contracts.py` ensures the field is present for
non-task chat turns.

## UI Behavior

### In-conversation search (Cmd+F)

- A magnifier `<Search />` button is added to the chat header action row,
  next to the existing `Info` button. Clicking it (or pressing
  `Cmd/Ctrl+F` while focused on the chat route) opens `ChatSearchBar.svelte`
  mounted under the header.
- The bar holds: query input, match counter (`1 of N`), prev/next arrows,
  case + diacritic toggles, and a "Search server" affordance that becomes
  visible when (a) local matches are zero AND `history_truncated` is true,
  or (b) the user explicitly clicks it.
- Client-side pass uses `Intl.Collator` for accent-insensitive substring
  matching over the loaded `timeline` (full array). Results map to
  timeline item IDs; the chat page DOM exposes `data-message-id={item.id}`
  on every wrapper so jump-to-match is a `querySelector + scrollIntoView`.
- When a match falls outside the windowed `displayedTimeline`, the page
  reduces `visibleStartIndex` to include it before scrolling, reusing the
  existing `loadOlder` scroll-anchor preservation. If history is not yet
  fully loaded, `loadHistory()` finishes first.
- Highlighting: a small post-render walker in `ChatMessage.svelte` wraps
  `<mark>` around matching text nodes; reactive on `[item.html, query]`.
- Server fallback hits `POST /api/v1/search/conversation/{id}`, returns
  match `event_seq`s, and the page navigates to each (loading older
  history if needed).
- Search scope: `user_message` + `assistant_message` only. Tool calls,
  thinking blocks, delegation cards, workflow notices, and compaction
  cards are skipped.

### Global sidebar search (explicit submit)

- The existing sidebar title-filter input is **promoted in place** to a
  full search. Magnifier icon is preserved; placeholder reads
  "Search conversations..." when search is enabled, "Filter titles..."
  when it is disabled.
- Behavior is **explicit submit** on `Enter` (or via the small "Search"
  submit button next to the input) — typing alone never issues a request.
  A subtle hint ("Press Enter to search") appears below the input when
  the typed text differs from the last submitted query.
- On submit, the page calls `POST /api/v1/search/conversations` with the
  query text and the sidebar's structural filters (agent, status,
  channel) attached. Results render in a new "Search results" group
  above the regular conversation list, showing title + agent chip +
  snippet (with `<mark>`) + match seq + last-message-at.
- Clicking a result navigates to the conversation, opens the in-
  conversation search bar pre-seeded with the same query, and jumps to
  the clicked seq.
- When search is disabled (Intaris flag off, or health endpoint reports
  `enabled=false`), the input falls back to the original client-side
  title filter and a small "Content search disabled" hint replaces the
  submit button.

### Settings card

`Settings → System` gains a small **Search** card surfacing
`/api/v1/search/health`: enabled flag, lexical backend in use, vector
backend (or "disabled"), embedding model, last index timestamp, queue
depth, backfill status with progress bar, and a "Reindex" button that
calls Intaris reindex via a Cognis admin proxy. The card is read-only
when search is disabled and links to Intaris docs.

### Intaris UI

Intaris also surfaces search natively — the feature is not Cognis-only:

- A new `/search` page in the Intaris UI with query input, filters
  (agent, session, alignment, risk, kind, time range, mode), grouped-
  by-session and flat match views, and a backend status chip.
- A search box on the existing session detail page (server-backed when
  enabled, client-side fallback when disabled).
- A `Settings → Search` admin panel: feature flag toggle (read-only when
  set via env), backend selection, embedding model, dimension, Qdrant
  URL/path/api key, reindex trigger with progress, and a "Test query"
  sandbox.

## API Surface

### Cognis additions

```
GET    /api/v1/search/health                           → Search availability/health (proxied)
POST   /api/v1/search/conversations                    → Cross-conversation search (joined with Cognis metadata)
POST   /api/v1/search/conversation/{conversation_id}   → In-conversation server fallback
POST   /api/v1/system/search/reindex                   → Admin: trigger Intaris reindex
GET    /api/v1/system/search/reindex/{job_id}          → Admin: poll reindex progress
```

All routes require JWT auth and scope by caller `user_email`. The admin
reindex routes additionally require admin role.

### Intaris additions

```
POST   /api/v1/search                       → Flat match list with snippets
POST   /api/v1/search/sessions              → Aggregated by session (top match + count)
GET    /api/v1/search/health                → Backend health, queue depth, backfill state
POST   /api/v1/search/reindex               → Enqueue backfill (scope: user or all)
GET    /api/v1/search/reindex/{job_id}      → Job status
GET    /api/v1/search/config                → Admin-only: resolved backend config
```

## Configuration

### Intaris environment variables

| Variable | Default | Description |
|---|---|---|
| `INTARIS_SEARCH_ENABLED` | `true` | Master feature flag. When `false`, all `/api/v1/search/*` routes return 404 and indexer does not start. |
| `INTARIS_SEARCH_LEXICAL_BACKEND` | `auto` | `auto` (PG-FTS or FTS5 trigram per dialect) or `like` (degraded fallback). |
| `INTARIS_SEARCH_VECTOR_BACKEND` | `disabled` | `disabled` \| `pgvector` \| `qdrant`. |
| `INTARIS_SEARCH_QDRANT_URL` | — | URL (`http(s)://...`) for shared deployments, or a local path (`file:///...` or absolute path) for single-user quickstart. Single setting; deployment chooses. |
| `INTARIS_SEARCH_QDRANT_API_KEY` | — | Optional Qdrant auth. |
| `INTARIS_SEARCH_QDRANT_COLLECTION` | `intaris-events` | |
| `INTARIS_SEARCH_EMBEDDING_MODEL` | — | LiteLLM id (e.g. `openai/text-embedding-3-small`, `ollama/bge-m3`). Required for Tier 2. |
| `INTARIS_SEARCH_EMBEDDING_DIM` | `1536` | Embedding dimension; mismatch with model triggers backfill. |
| `INTARIS_SEARCH_EMBEDDING_BATCH_SIZE` | `32` | |
| `INTARIS_SEARCH_HYBRID_ALPHA` | `0.5` | RRF weighting between lexical and vector. |
| `INTARIS_SEARCH_BACKFILL_BATCH_SIZE` | `200` | Sessions per batch during reindex. |
| `INTARIS_SEARCH_MAX_TEXT_BYTES` | `8192` | Per-event truncation before embedding/indexing. |

### Quickstart

For single-user SQLite quickstart deployments:

```
INTARIS_SEARCH_ENABLED=true
INTARIS_SEARCH_VECTOR_BACKEND=qdrant
INTARIS_SEARCH_QDRANT_URL=~/.intaris/qdrant   # local-mode path
INTARIS_SEARCH_EMBEDDING_MODEL=ollama/bge-m3
INTARIS_SEARCH_EMBEDDING_DIM=1024
```

For shared cloud-style deployments, point `INTARIS_SEARCH_QDRANT_URL` at
the same Qdrant Mnemory uses; collection isolation prevents conflicts.

For pure lexical (no embedding cost / no extra services):

```
INTARIS_SEARCH_ENABLED=true
INTARIS_SEARCH_VECTOR_BACKEND=disabled
```

This still gives diacritic-tolerant substring search via Tier 1.

## Failure Modes

| Failure | Behavior |
|---------|----------|
| Indexer crash / restart | Outbox drained on next start; live writes succeed in the meantime. |
| Vector backend unhealthy | Queries degrade to lexical-only; response carries `X-Search-Degraded: vector_unavailable`. Tier 1 indexing unaffected. |
| Embedding provider rate-limit | Exponential backoff in indexer; live writes never block. |
| `unaccent` extension missing on PG | Tier 1 falls back to non-unaccented `tsvector`; client-side `text_normalized` covers diacritics. |
| Qdrant local-mode path unwritable | Vector backend marked unhealthy; lexical-only continues. |
| Search feature flag off | Intaris returns 404 from `/api/v1/search/*`; Cognis hides UI affordances and the LLM tools return `{"error": "search_disabled"}`. |
| Cognis ↔ Intaris JWT failure | Fail-soft: empty results + warning, never propagate as a chat-blocking error. |

## Observability

- Metrics (Intaris): `intaris_search_outbox_depth`,
  `intaris_search_index_latency_ms`, `intaris_search_embed_latency_ms`,
  `intaris_search_query_latency_ms{mode=...}`,
  `intaris_search_query_errors_total{backend=...}`,
  `intaris_search_backfill_progress`.
- Metrics (Cognis): `cognis_search_proxy_latency_ms`,
  `cognis_search_proxy_errors_total`, `cognis_search_health_cached_hits`.
- Logs allowlist: IDs (`session_id`, `event_seq`, `index_id`, `job_id`),
  backend names, mode, latency, error categories. **Never** log `q`,
  `text`, `snippet`, or any event content.
- Structured headers: `X-Search-Backend-Lexical`,
  `X-Search-Backend-Vector`, `X-Search-Mode-Used`, `X-Search-Degraded`.

## Privacy and Security

- Caller identity always derives from JWT `sub`. Body-supplied `user_id`
  is ignored.
- Admin role does not grant cross-user search access.
- Shared agents (`agent_grants`) do not expand search scope. A grantee
  searches only their own conversations with that agent.
- Snippet generation runs server-side; clients never receive un-redacted
  text outside the matching event. Tool/audit content is intentionally
  excluded from the index, which keeps tool-argument secrets out of
  search output.
- Retention: when a session is purged, an `op="delete_session"` outbox
  row cascades to `event_search_index`, `event_search_embeddings`, and
  Qdrant points by filter.

## Open follow-ups (not in this stage)

- Search-backed memory hints: surface `summary_compacted` matches as
  Mnemory recall candidates. Requires Mnemory glue.
- Cross-account global search for federated deployments. Out of scope
  pending the federation design (`08-federation.md`).
- Audit-log search UI in Cognis. Today only Intaris exposes this; Cognis
  could mirror it but does not need to for this spec.
- Indexed tool-event search: deliberately excluded; revisit only if a
  concrete user need emerges. The audit log already supports structured
  queries on tool calls.
