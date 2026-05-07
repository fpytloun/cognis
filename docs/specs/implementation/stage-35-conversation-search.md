# Stage 35: Conversation Search

## Status

PLANNED

## Goal

Ship the work described in
[`../32-conversation-search.md`](../32-conversation-search.md) in eight
reviewable phases:

1. **Intaris schema and bootstrap** — `event_search_index`,
   `event_search_embeddings`, `search_outbox`, `search_state` tables;
   bootstrap `_ensure_*` helpers; Alembic migration.
2. **Intaris indexer + lexical Tier 1** — outbox writer hooks, indexer
   worker, Postgres FTS path (tsvector + pg_trgm), SQLite FTS5 trigram
   path, snippet generator.
3. **Intaris query API** — `POST /search`, `POST /search/sessions`,
   `GET /search/health`, `POST /search/reindex`, JWT scoping, RRF
   fusion module, mode resolution, response shapes.
4. **Intaris vector Tier 2** — pgvector backend, Qdrant URL/local-mode
   backend, embedding queue, model/dimension change detection,
   auto-backfill on swap.
5. **Intaris UI surfaces** — `/search` page, session-detail in-page
   search, `Settings → Search` admin panel, quickstart docs update.
6. **Cognis provider + proxy routes** — `IntarisProvider.search`,
   `search_sessions`, `search_health`; circuit breaker family `search`;
   `cognis/api/routes/search.py` (`/health`, `/conversations`,
   `/conversation/{id}`); `conversation_id` in `runtime_metadata` +
   `RuntimeAccessContext`. Cognis reindex proxy is deferred; operators use
   Intaris admin/API for backfills.
7. **Cognis UI surfaces** — `ChatSearchBar.svelte` with Cmd+F + magnifier
   + client-first/server-fallback search; sidebar promote-to-search with
   explicit submit; settings card surfacing search health.
8. **Cognis LLM tools** — `list_conversations`, `search_conversations`,
   `read_conversation_messages` with anchor-based pagination; profile
   group wiring; in-process executor handler registration; tests.

Each phase ships with migrations + bootstrap helpers (where needed) +
tests + UI in the same PR. Phases 35.1–35.5 land in Intaris and can be
reviewed independently of Cognis. Phases 35.6–35.8 land in Cognis and
can be drafted in parallel with 35.3+ once the Intaris API shape is
stable.

## Dependencies

- [`../32-conversation-search.md`](../32-conversation-search.md)
- [`../05-integrations.md`](../05-integrations.md) (`IntarisProvider`,
  JWT auth, retry + circuit breaker patterns)
- [`../06-tool-system.md`](../06-tool-system.md) (Pattern B handler-
  registered tools, profile groups, `ToolExecutionContext`)
- [`../09-ui-ux.md`](../09-ui-ux.md) (chat workspace, sidebar, keyboard
  shortcut bus)
- [`../10-api-spec.md`](../10-api-spec.md) (REST surface conventions)
- [`../28-agent-sharing.md`](../28-agent-sharing.md) (no admin bypass for
  user-owned resources; `agent_grants` does not expand search scope)
- Stages 0–16, 30–33 complete (foundational infra including agent
  sharing, projects, deliverables, voice mode does not block this stage)

## Scope

### In scope

**Intaris:**

- `event_search_index`, `event_search_embeddings`, `search_outbox`,
  `search_state` tables with Alembic migrations and bootstrap
  `_ensure_*` helpers.
- Tier 1 lexical backends: Postgres `tsvector` generated columns over
  canonical tables (with immutable `unaccent` wrapper when available) plus
  `pg_trgm`; SQLite canonical-table `LIKE` over `intaris_fold` normalized
  text.
- Tier 2 vector backends: `pgvector` and `qdrant`. Qdrant URL setting
  accepts both server URL and local-mode path.
- Outbox-backed indexer worker with synchronous Tier 1 + asynchronous
  Tier 2; circuit breaker on embedding provider; backoff/retry.
- Live indexing/backfill hooks for canonical `reasoning`, `intention`, and
  `summary` rows. Raw user/assistant messages are not Intaris search kinds.
- Query orchestrator with mode resolution, RRF fusion, snippet
  generation, opaque cursor pagination.
- `POST /api/v1/search`, `POST /api/v1/search/sessions`,
  `GET /api/v1/search/health`, `POST /api/v1/search/reindex`,
  `GET /api/v1/search/reindex/{job_id}`, `GET /api/v1/search/config`.
- Backfill worker with auto-trigger on schema/backend/model change.
- Feature flag `INTARIS_SEARCH_ENABLED` (default `true`).
- `INTARIS_SEARCH_*` env var family.
- Intaris UI: `/search` page, session-detail in-page search,
  `Settings → Search` admin panel.
- Quickstart docs updated for SQLite + local-mode Qdrant.
- Tests: unit (lexical normalizer, fusion, snippet, outbox replay),
  integration (PG with/without unaccent and pgvector, SQLite + FTS5,
  Qdrant local-mode + server-mode), perf smoke (10k sessions × 200
  events).

**Cognis:**

- `IntarisProvider.search`, `search_sessions`, `search_health` methods
  with new circuit breaker family `search` (fail-soft).
- `cognis/api/routes/search.py` with `/health` (cached 30s),
  `/conversations` (proxy + join), `/conversation/{id}` (in-conversation
  fallback). Admin reindex proxy is deferred.
- `conversation_id` propagated through `RuntimeAccessContext` and
  `runtime_metadata` so tool handlers can read the current conversation
  without a DB hop.
- `ChatSearchBar.svelte` mounted under the chat header with magnifier
  button in the header action row, Cmd/Ctrl+F shortcut scoped to chat
  route, client-side substring search with `Intl.Collator` + jump-to-
  match + window expansion + server fallback.
- Sidebar input promoted to explicit-submit search (Enter / submit
  button); "Search results" group rendered above the regular conversation
  list when search returns matches.
- `data-message-id={item.id}` added to message wrappers and inside
  `ChatMessage.svelte` so jump-to-match works.
- `Settings → System` Search card surfacing `/api/v1/search/health`.
- Three LLM tools (`list_conversations`, `search_conversations`,
  `read_conversation_messages`) in `cognis/tools/builtin/conversations.py`,
  wired through `static_tool_definitions()`, `_build_handler_map()`, and
  in-process executor registration. `category="conversations"`,
  `read_only=True`, `profile_group="research"`.
- Lineage walker (root + compaction children + sub-sessions) used by
  `read_conversation_messages` and `search/conversation/{id}`.
- Tests: unit (lineage walking, ownership filter, anchor pagination),
  contract (Cognis ↔ Intaris with search enabled and disabled), UI
  (Vitest for `chat-search.ts`, component test for `ChatSearchBar`).

### Out of scope

- Tool-event indexing (tool calls, tool results, evaluations,
  delegations, lifecycle, checkpoints, reasoning). Use Intaris audit
  endpoints for those.
- Search-backed Mnemory recall hints. Future follow-up.
- Cross-account / federated search. Pending federation spec.
- Audit-log search UI in Cognis (Intaris already has it).
- Per-user TTS-style cost quota for embedding calls.
- A Cognis-side command palette built around Cmd+K. Not blocked, but
  not part of this stage.
- Migrating away from the existing client-side title filter when search
  is **disabled**; we intentionally fall back to it.

## Phased rollout inside this stage

| Phase | Name | Notes |
|-------|------|-------|
| 35.1 | Intaris schema + bootstrap | `event_search_index`, `event_search_embeddings`, `search_outbox`, `search_state` |
| 35.2 | Intaris indexer + Tier 1 | Outbox writer hooks, worker, PG FTS + SQLite FTS5 |
| 35.3 | Intaris query API | `/search`, `/search/sessions`, `/search/health`, `/search/reindex` |
| 35.4 | Intaris vector Tier 2 | pgvector, Qdrant URL/local-mode, backfill |
| 35.5 | Intaris UI + quickstart docs | `/search` page, session-detail search, Settings → Search |
| 35.6 | Cognis provider + proxy routes | `IntarisProvider.search*`, search routes, `conversation_id` plumbing |
| 35.7 | Cognis UI surfaces | Cmd+F bar, sidebar promote-to-search, settings card |
| 35.8 | Cognis LLM tools | `list_conversations`, `search_conversations`, `read_conversation_messages` |

## Deliverables

### 35.1 Intaris schema + bootstrap

DB:

- `intaris/intaris/db/migrations/versions/<rev>_search_tables.py` — create
  `event_search_index`, `event_search_embeddings`, `search_outbox`,
  `search_state`. PG-specific blocks for `tsvector` generated column,
  GIN indices, optional `unaccent` extension probe. SQLite-specific block
  for FTS5 contentless table + sync triggers.
- `intaris/intaris/bootstrap.py` (or equivalent) — idempotent
  `_ensure_event_search_index_table`,
  `_ensure_event_search_embeddings_table`,
  `_ensure_search_outbox_table`, `_ensure_search_state_row`. Registered
  in the Intaris equivalent of `run_schema_bootstrap()`.
- Bootstrap branches on dialect: PG path probes `unaccent` extension and
  records the result in `search_state`; SQLite path creates the FTS5
  shadow table and sync triggers.

Domain types:

- `intaris/intaris/search/types.py` — `IndexedKind` enum,
  `SearchMatch`, `SearchSessionMatch`, `SearchHealth`, `SearchConfig`
  Pydantic models.

Tests:

- `tests/unit/test_search_schema.py` — bootstrap idempotence on PG and
  SQLite, FTS5 shadow table created, generated column present on PG,
  unaccent probe records correct backend marker.

### 35.2 Intaris indexer + lexical Tier 1

Indexer:

- `intaris/intaris/search/indexer.py` — `IndexerWorker` async task with
  `start()`/`stop()`, polls `search_outbox` ordered by
  `(next_attempt_at, id)`, processes ops `index_event`,
  `upsert_session_field`, `index_summary`, `delete_session`. Synchronous
  Tier 1 write per op; failure marks the row for retry with exponential
  backoff (capped). `embed` op is created in this phase but is a no-op
  until 35.4.

Hooks:

- `intaris/intaris/events/store.py` (or equivalent) — on successful
  append commit, write a `search_outbox` row in the same transaction
  for events of type `user_message` or `assistant_message`.
- `intaris/intaris/session.py` — on intention or title update, write
  `upsert_session_field` outbox row.
- `intaris/intaris/summaries.py` — on `session_summaries` and
  `agent_summaries` insert, write `index_summary` outbox row.

Lexical backends:

- `intaris/intaris/search/lexical/__init__.py` — backend selector based
  on dialect + `INTARIS_SEARCH_LEXICAL_BACKEND`.
- `intaris/intaris/search/lexical/postgres.py` — write helpers (insert
  with generated `tsv` column) and read helpers (`ts_rank` + `ts_headline`,
  `pg_trgm` similarity fallback).
- `intaris/intaris/search/lexical/sqlite.py` — write helpers (insert into
  `event_search_index`; FTS5 stays in sync via triggers) and read helpers
  (`bm25(event_search_fts)` + `snippet()`).
- `intaris/intaris/search/snippet.py` — formatting helpers used by both
  backends, normalizing snippet `<mark>` markers and clamping length.

Settings + flag:

- `intaris/intaris/config.py` — add `INTARIS_SEARCH_ENABLED`,
  `INTARIS_SEARCH_LEXICAL_BACKEND`, `INTARIS_SEARCH_BACKFILL_BATCH_SIZE`,
  `INTARIS_SEARCH_MAX_TEXT_BYTES` resolution.
- App startup gates indexer and routes registration on the flag.

Tests:

- `tests/unit/test_search_outbox.py` — outbox write semantics,
  retry/backoff, attempt counters, drop on max retries.
- `tests/unit/test_search_lexical_postgres.py` — insert + query +
  snippet on a real PG container; verify diacritic folding via
  `unaccent`; verify trigram fallback for short queries.
- `tests/unit/test_search_lexical_sqlite.py` — same for FTS5 trigram.
- `tests/unit/test_search_event_hooks.py` — append produces outbox
  rows only for indexed event types; tool calls and other types do
  not.

### 35.3 Intaris query API

Service:

- `intaris/intaris/search/service.py` — orchestrator with
  `search(...)`, `search_sessions(...)`, `health()`, `reindex(...)`.
  Resolves effective mode against backend health, dispatches to lexical
  + (optionally) vector, applies RRF, returns Pydantic match objects.
- `intaris/intaris/search/fusion.py` — Reciprocal Rank Fusion helper
  (`k=60` default, configurable α weighting).

Routes:

- `intaris/intaris/api/search.py` — `POST /api/v1/search`,
  `POST /api/v1/search/sessions`, `GET /api/v1/search/health`,
  `POST /api/v1/search/reindex`, `GET /api/v1/search/reindex/{job_id}`,
  `GET /api/v1/search/config` (admin-only). Route registration is
  conditional on `INTARIS_SEARCH_ENABLED`.
- JWT auth: `user_id` always derived from the `sub` claim and forced
  into filters; body-supplied `user_id` is ignored.
- Cursor format: opaque base64 of `{"score":..., "id":..., "rank":...}`.
  Helpers in `intaris/intaris/search/cursor.py`.

Reindex worker:

- `intaris/intaris/search/backfill.py` — backfill orchestrator. Walks
  sessions in `last_activity_at DESC` order, pages through events, and
  enqueues outbox rows. Updates `search_state.backfill_*` columns.
  Auto-trigger when `search_state` lexical schema or vector backend or
  embedding model differs from the current resolved config at startup.

Headers + observability:

- Responses set `X-Search-Backend-Lexical`, `X-Search-Backend-Vector`,
  `X-Search-Mode-Used`, and `X-Search-Degraded` when applicable.

Tests:

- `tests/unit/test_search_query_api.py` — JWT scoping, mode resolution,
  pagination cursor stability, snippet correctness.
- `tests/unit/test_search_fusion.py` — RRF fusion math; lexical-only +
  vector-only paths; tie-break determinism.
- `tests/unit/test_search_reindex.py` — backfill enqueues outbox rows,
  status endpoint returns progress, auto-trigger on schema bump.
- `tests/contract/test_search_api_contract.py` — request/response shapes
  match the OpenAPI spec; PG and SQLite both pass.

### 35.4 Intaris vector Tier 2

Vector backends:

- `intaris/intaris/search/vector/__init__.py` — backend selector.
- `intaris/intaris/search/vector/pgvector.py` — `embed_and_store(...)`,
  `search(...)` with HNSW cosine; column-type recreate on dimension
  change.
- `intaris/intaris/search/vector/qdrant.py` — accepts both URL and
  local-mode path. Local-mode uses
  `QdrantClient(path=...)`; URL-mode uses `QdrantClient(url=..., api_key=...)`.
  Lazy imports `qdrant-client[fastembed]` (optional dep group
  `intaris[search-qdrant]`).
- `intaris/intaris/search/vector/disabled.py` — no-op backend used when
  `INTARIS_SEARCH_VECTOR_BACKEND=disabled`.

Embedding pipeline:

- `intaris/intaris/search/embeddings.py` — LiteLLM-backed embedding
  client; batched calls (`INTARIS_SEARCH_EMBEDDING_BATCH_SIZE`); per-
  provider circuit breaker; per-row retry through outbox.
- Indexer worker now processes `embed` ops by reading `event_search_index`
  rows that lack an embedding row, embedding in batches, and writing to
  the configured vector backend.

Mode + fusion:

- `service.py.search()` extends to call vector backend when
  `mode != "lexical"` and the backend is healthy. RRF in `fusion.py`
  consumes both rank lists.

Auto-backfill:

- Startup compares `search_state.vector_backend / vector_model /
  vector_dim` with resolved config. On mismatch, enqueue full backfill
  for vector index (lexical untouched).

Tests:

- `tests/unit/test_search_vector_pgvector.py` — embed + store + query;
  dimension change triggers re-embed.
- `tests/unit/test_search_vector_qdrant_local.py` — local-mode path;
  CRUD; collection auto-create.
- `tests/unit/test_search_vector_qdrant_server.py` — URL mode against a
  containerized Qdrant.
- `tests/unit/test_search_hybrid_query.py` — RRF combines lexical and
  vector; degraded mode when vector is unhealthy.
- `tests/unit/test_search_embedding_circuit_breaker.py` — embedding
  failures back off and never block live writes.

### 35.5 Intaris UI + quickstart docs

UI:

- `intaris/ui/src/routes/search/+page.svelte` — query input, filters
  (agent, session, alignment, risk, kind, time range, mode), grouped-
  by-session and flat match views, backend status chip, pagination.
- `intaris/ui/src/routes/sessions/[id]/+page.svelte` — in-page search
  bar with server-backed search when feature is enabled, client-side
  substring fallback when disabled.
- `intaris/ui/src/routes/settings/search/+page.svelte` — admin panel:
  feature flag toggle (read-only display when set via env), backend
  selection, embedding model, dimension, Qdrant URL/path/api key,
  reindex trigger with progress polling, "Test query" sandbox.

Docs:

- `intaris/docs/quickstart.md` — add an "Enable search" section showing
  the SQLite + local-mode Qdrant + Ollama bge-m3 path and the
  lexical-only fallback.
- `intaris/docs/operations/search.md` (new) — backend choices, scaling
  notes, retention behavior, metrics.

Tests:

- Vitest/Playwright component tests for `/search` page and session-
  detail search bar.
- Settings panel snapshot test.

### 35.6 Cognis provider + proxy routes

Provider:

- `cognis/providers/guardrails/intaris.py` — add `search`,
  `search_sessions`, `search_health` methods. Wrap with retry helper. Add a new
  circuit breaker family `"search"` with **fail-soft** behavior (returns
  empty results + warning, never raises into the chat path).
- `cognis/providers/guardrails/protocol.py` — extend Protocol with the
  new methods.

Routes:

- `cognis/api/routes/search.py` (new):
  - `GET /api/v1/search/health` — proxies to Intaris with a 30 s in-
    process cache.
  - `POST /api/v1/search/conversations` — body
    `{q, filters: {agent_id?, project_id?, status?, from_ts?, to_ts?}, kinds?, mode?, limit?, cursor?}`.
    Forwards to Intaris `search_sessions`, joins to Cognis
    `conversations` (resolves `intaris_session_id → conversation_id`,
    drops non-owned, applies `project_id`/`status` filters), returns
    `{matches: [...], next_cursor, backend}`.
  - `POST /api/v1/search/conversation/{conversation_id}` — verifies
    ownership, walks lineage, forwards to Intaris `search` with
    `session_ids=[...]`, returns the flat match list.
  - Reindex proxy is deferred. Use Intaris operations UI/API for manual
    reindex.

Lineage walker:

- `cognis/store/queries.py` — `list_conversation_session_ids(conversation_id)`
  helper that returns the chronological list of `intaris_session_id`s
  reachable from the conversation root (root + compaction children +
  sub-sessions).

Runtime metadata:

- `cognis/runtime_context.py` — extend `RuntimeAccessContext` with
  `conversation_id: str | None`. Populate at every construction site:
  turn scheduler, workflow engine, channel inbound, executor WS,
  background task harness.
- `cognis/api/runtime_support.py` — include `conversation_id` in the
  `runtime_metadata` dict assembled per tool call.
- `tests/unit/test_api_contracts.py` — invariant: `conversation_id` is
  present in `runtime_metadata` for non-task chat turns.

Wiring + auth:

- `cognis/api/app.py` — register `routes.search.router` under `/api/v1`.
- All routes require JWT auth; admin routes additionally require admin
  role. Body `user_id` is never read.

Tests:

- `tests/unit/test_search_routes.py` — health caching, ownership join,
  non-owned matches dropped, `project_id`/`status` filters applied,
  fail-soft when Intaris circuit is open.
- `tests/unit/test_runtime_access_context.py` — `conversation_id`
  populated and threaded.
- `tests/contract/test_intaris_search_contract.py` — round-trip against
  a running Intaris with search enabled and disabled.
- `tests/unit/test_ui_contract_sync.py` — TS interfaces in
  `ui/src/lib/types/api.ts`.

### 35.7 Cognis UI surfaces

State and helpers:

- `ui/src/lib/chat-search.ts` (new) — `chatSearchOpen` writable,
  `requestOpenChatSearch()` / `onOpenChatSearchRequest(handler)` event
  bus, pure `findMatches(timeline, query, options)` function using
  `Intl.Collator` for diacritic-folded substring matching, `MatchRef`
  type with `{ id, sessionId, seq?, snippetRange }`.

Components:

- `ui/src/lib/components/ChatSearchBar.svelte` (new) — query input,
  match counter, prev/next, case + diacritic toggles, "Search server"
  button. Mounts under the chat header. Mobile path uses `Sheet.svelte`
  with `side="bottom"` to integrate with safe-area insets.
- `ui/src/lib/components/ChatMessage.svelte` — add post-render walker
  that wraps `<mark>` around matching text nodes; reactive on
  `[item.html, query]`. Add `data-message-id={item.id}` to the message
  wrapper.

Chat page:

- `ui/src/routes/(app)/chat/[conversationId]/+page.svelte`:
  - Add magnifier `<Search />` button to the header action row next to
    `Info`.
  - Mount `ChatSearchBar.svelte` under the header when
    `$chatSearchOpen`.
  - Add `data-message-id={item.id}` to the timeline `{#each}` wrapper
    around each renderer.
  - Window expansion logic: when a match is outside `displayedTimeline`,
    reduce `visibleStartIndex` to include it before scrolling.
  - Server fallback: when local matches are zero AND `history_truncated`
    is true (or the user clicks "Search server"), call
    `POST /api/v1/search/conversation/{id}`. Display all returned matching
    parts. Prioritize `reasoning` hits for nearest-message/time navigation,
    use `intention` as session/section context, and show `summary` as the
    lowest-priority fallback.

Sidebar:

- Promote the existing title-filter input to explicit-submit search:
  - `Enter` (or click on a small submit button) issues
    `POST /api/v1/search/conversations` with the typed `q` plus
    structural filters (agent, status, channel) in the body.
  - Render results in a "Search results" group above the regular
    conversation list. Each result row: title + agent chip + snippet
    (with `<mark>`) + match seq + last-message-at.
  - Clicking a result navigates to the conversation, opens
    `ChatSearchBar` pre-seeded with the same query, runs in-conversation
    search immediately, and renders all matching parts. The clicked result is
    selected as the initial match when available.
  - When `/search/health` reports disabled, the input falls back to the
    original client-side title filter and shows a "Content search
    disabled" hint.

Keyboard shortcuts:

- `ui/src/lib/shortcuts.ts` — add `CHAT_SEARCH_OPEN_EVENT`,
  `requestOpenChatSearch`, `onOpenChatSearchRequest`. Update help dialog
  copy in `ShortcutHelp.svelte`.
- `ui/src/routes/(app)/+layout.svelte` — `handleGlobalShortcuts` adds
  `Cmd/Ctrl+F` handler scoped to the chat route. `Escape` closes the
  search bar before any other behavior.

Settings card:

- `ui/src/routes/(app)/settings/+page.svelte` — add "Search" card under
  System surfacing `/api/v1/search/health`: enabled flag, backends in
  use, embedding model, last index timestamp, queue depth, backfill
  status with progress bar, "Reindex" button (admin only, calls
  Intaris search health. Manual reindex remains in Intaris operations UI/API.

Tests:

- `ui/src/lib/chat-search.test.ts` — `findMatches` correctness across
  diacritics, case, regex toggles.
- `ui/src/lib/components/ChatSearchBar.test.ts` — match counter,
  jump-to-match, server fallback trigger, escape handling.
- Component snapshot for the settings Search card.

### 35.8 Cognis LLM tools

Tool definitions:

- `cognis/tools/builtin/conversations.py` (new) — `LIST_CONVERSATIONS_TOOL`,
  `SEARCH_CONVERSATIONS_TOOL`, `READ_CONVERSATION_MESSAGES_TOOL`,
  `conversation_tools()` factory, `is_conversation_tool(name)` helper.
  All `category="conversations"`, `read_only=True`,
  `profile_group="research"`.

Handlers:

- `cognis/tools/builtin/conversations.py` — `build_conversation_tool_handlers(session_factory, intaris)`:
  - `list_conversations_handler` — uses
    `cognis.store.queries.list_conversations` with the caller's
    `user_email` and supported filters.
  - `search_conversations_handler` — calls
    `intaris.search_sessions(...)`, joins with Cognis `conversations`,
    drops non-owned matches, applies `project_id` filter. Returns
    `{"error": "search_disabled"}` when health reports disabled.
  - `read_conversation_messages_handler` — verifies ownership;
    resolves the lineage via `list_conversation_session_ids`; routes
    on the `anchor` variant (`latest`, `from_start`, `around`,
    `after`, `before`) or `cursor`; issues parallel `read_events` calls
    with seq filters; merges by timestamp; truncates content per
    `include_content_truncation`; returns `{events, page:{...}}`.

Wiring:

- `cognis/api/runtime_support.py` — add `*conversation_tools()` to
  `static_tool_definitions()` and
  `handlers.update(build_conversation_tool_handlers(session_factory, intaris))`
  in `_build_handler_map()`.
- `cognis/providers/executor/in_process.py::_register_handlers()` —
  mirror the same handler-map plumbing.
- `cognis/core/tool_router.py` — `category="conversations"` falls into
  the existing `LOCAL` route; no new branch needed (Pattern B).

Anchor + cursor:

- `cognis/tools/builtin/_conversation_anchors.py` (or similar) — pure
  helpers for cursor encode/decode (`{sid, seq, dir}` opaque base64),
  anchor variant validation, and lineage windowing.

Profile group:

- `cognis/core/step_profiles.py` — extend the `research` profile group
  to include the `conversations` category so the tools are available
  in research and coding workflow steps by default. `unrestricted`
  always exposes them.

Tests:

- `tests/unit/test_conversation_tools.py` — list/search/read happy
  paths; ownership filter; cursor stability under concurrent appends;
  `around`/`after`/`before` anchor correctness; `kinds` filter; content
  truncation toggle; search-disabled error shape.
- `tests/unit/test_conversation_lineage.py` — root + compaction
  children + sub-sessions ordering; merge by timestamp.
- `tests/unit/test_api_contracts.py` — tool argument schemas round-trip
  through the harness validator.
- `tests/contract/test_conversation_tools_contract.py` — end-to-end
  against running Intaris with seeded sessions.

## Acceptance criteria

- All deliverables in 35.1–35.8 land with migrations + bootstrap +
  tests + UI in the same PR per phase.
- Intaris bootstrap idempotently creates `event_search_index`,
  `event_search_embeddings`, `search_outbox`, and `search_state` on
  both PG and SQLite.
- Live indexing produces an `event_search_index` row for every
  `user_message` / `assistant_message` event, plus upserts for
  `intention`/`title` and inserts for window/compacted/agent summaries.
  Tool calls, evaluations, and lifecycle events do not produce rows.
- Tier 1 lexical search returns results with `<mark>`-highlighted
  snippets on PG and SQLite, with diacritic folding active by default.
- When `INTARIS_SEARCH_VECTOR_BACKEND=pgvector` or `qdrant` and a
  valid embedding model is configured, hybrid mode produces ranked
  results that include vector-only matches not reachable via lexical;
  unhealthy vector backend transparently degrades to lexical with
  `X-Search-Degraded: vector_unavailable`.
- `INTARIS_SEARCH_ENABLED=false` causes all `/api/v1/search/*` routes
  to return 404 and prevents indexer startup.
- Backfill auto-triggers on schema/backend/model change; progress
  endpoint reports current/total/state.
- Cognis `/api/v1/search/conversations` returns matches joined with
  conversation titles and ownership; non-owned conversations are never
  returned, including for admin callers.
- `RuntimeAccessContext.conversation_id` is populated everywhere it is
  constructed and threads through to `runtime_metadata`.
- In-conversation Cmd+F highlights matches with `<mark>`, jumps to a
  match (expanding the timeline window if necessary), and falls through
  to server search when the conversation history is truncated.
- Sidebar search submits on Enter only and renders a "Search results"
  group; clicking a result opens the chat at that match.
- All three LLM tools work end-to-end against a seeded conversation
  set; `read_conversation_messages` paginates forward and backward
  across a multi-session lineage with stable cursors;
  `search_conversations` returns
  `{"error": "search_disabled"}` (not a transient failure) when search
  is off.
- `tests/unit/test_api_contracts.py` and
  `tests/unit/test_ui_contract_sync.py` pass.
- Privacy/observability: no metric or log line contains `q`, `text`, or
  `snippet` content; only IDs, latencies, backend names, and error
  categories.

## Risks and mitigations

- **`unaccent` extension absent on PG.** Detected at startup; the
  generated `tsv` column falls back to `simple`-only and a Python-level
  `text_normalized` covers diacritics. Status surfaced in
  `search_state` and `/search/health`.
- **SQLite FTS5 trigram tokenizer unavailable.** Trigger on SQLite
  `< 3.34`. Bootstrap probes capability; falls back to FTS5 default
  tokenizer with a documented recall caveat. Operational docs require
  SQLite ≥ 3.34 for full feature support.
- **Embedding provider rate-limit / outage.** Per-provider circuit
  breaker; outbox backoff/retry; live writes never block on
  embeddings. Vector queries degrade transparently.
- **Embedding dimension mismatch on model swap.** `search_state`
  records last-seen `vector_dim` and `vector_model`. On change, the
  embeddings column is recreated and a backfill is auto-enqueued. UI
  surfaces backfill progress.
- **Qdrant local-mode path on unwritable filesystem.** Vector backend
  health check writes a probe collection; failure marks vector
  unhealthy and the indexer logs a warning. Lexical Tier 1 unaffected.
- **Cross-session lineage walking under heavy compaction.** Lineage
  helper is bounded by `MAX_LINEAGE_SESSIONS` (default 64). Beyond
  that, the helper paginates lineage itself and the caller can drill
  into specific sessions. Documented in tool descriptions.
- **`<mark>` injection from snippets.** Server returns snippets with
  literal `<mark>` markers; the client renders them via a constrained
  sanitizer that allows only `<mark>` and strips everything else, even
  on the in-conversation client-side highlight path.
- **Privacy regression from indexed text.** Tool args are not indexed;
  summaries are LLM-generated and have already passed Intaris content
  policy. A sanity-check unit test asserts that indexer never receives
  tool-call event payloads.
- **Sidebar UX confusion.** Title filter changes behavior when search
  is enabled. We mitigate with the placeholder change ("Search
  conversations..." vs "Filter titles..."), the explicit-submit hint,
  and a separate "Search results" results group so the regular list is
  not displaced.
- **Search disabled in middle of a session.** UI polls
  `/api/v1/search/health` infrequently (30 s) and degrades gracefully
  on the next render. LLM tools return a stable
  `{"error": "search_disabled"}` shape.

## Stage exit

Update the tracker in [implementation/README.md](README.md): Stage 35
DONE. Add a follow-up note that any new event types added to Intaris
must explicitly opt into indexing (deny-list by default) and that
`runtime_metadata["conversation_id"]` is the canonical way for tool
handlers to know the active conversation going forward.
