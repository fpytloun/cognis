# Optional artifact-linked Knowledgebases

Cognis Knowledgebases are an optional platform feature for indexing existing
Cognis artifacts into a configured vector backend and querying them with hybrid
retrieval. The feature is generic: a knowledgebase belongs to a user, links to
canonical Cognis artifacts, and stores only disposable derived chunks/vectors.

## Enablement

The API is present, but operational endpoints are hidden/unavailable unless both
requirements are met:

- `COGNIS_KNOWLEDGEBASE_VECTOR_BACKEND=qdrant`
- a model-routing entry exists for task type `embedding`

`GET /api/v1/knowledgebases/capabilities` always returns HTTP 200 and reports
backend, embedding, indexer, and Ask readiness; supported generic document
types; configured upload/index/chunk limits; and safe actionable notes.
`GET /api/v1/knowledgebases/health` remains available for operational health.
Operational routes retain 404/503-style unavailable behavior and never expose
configuration secrets.

Capability notes include conditions such as
`vector backend disabled`, `embedding route not configured`, or backend
dependency/connectivity failures. The background indexer is not started while
the feature is unhealthy.

## Configuration

```env
COGNIS_KNOWLEDGEBASE_VECTOR_BACKEND=qdrant
COGNIS_KNOWLEDGEBASE_QDRANT_URL=http://localhost:6333
COGNIS_KNOWLEDGEBASE_QDRANT_API_KEY=
COGNIS_KNOWLEDGEBASE_QDRANT_COLLECTION=cognis_knowledgebase_chunks
COGNIS_KNOWLEDGEBASE_INDEX_POLL_INTERVAL_SECONDS=5
COGNIS_KNOWLEDGEBASE_MAX_ARTIFACT_SIZE_MB=50
COGNIS_KNOWLEDGEBASE_MAX_CHUNKS_PER_ARTIFACT=2000
COGNIS_KNOWLEDGEBASE_EMBEDDING_BATCH_SIZE=32
```

Install optional dependencies with the `knowledgebase` extra for Qdrant and
DOCX support.

## Lifecycle and indexing

Owners can ingest generic documents directly with multipart
`POST /api/v1/knowledgebases/{id}/documents`, browse them with cursor pagination,
read document details and bounded textual source/extracted content, update
generic metadata/source paths, detach/reindex, inspect jobs/diagnostics, search,
and Ask. Existing attach-by-artifact APIs remain available for advanced use.

Multipart ingestion accepts up to the advertised batch limit using `files[]`
and optional matching `paths[]`, plus optional metadata JSON and
`conflict_policy=skip|replace|keep_both`. Paths are normalized relative POSIX
paths; absolute paths, traversal, empty segments, and backslashes are rejected.
Each file is independently committed and returns a typed
`created|updated|unchanged|skipped|failed` result. Canonical source artifacts are
immutable, owner-scoped, non-expiring records. Replacement stages a new
generation while the last good active generation remains searchable.
`keep_both` deterministically adds ` (2)`, ` (3)`, and so on before the
extension. ZIP expansion, watchers, connectors, source-specific parsers, and
automatic source deletion are intentionally absent. Version-one bounded YAML
frontmatter is the only generic metadata envelope with extraction semantics.
The API enforces per-file and conservative aggregate byte budgets before any
artifact/job persistence, plus bounded metadata/path fields. Index-producing
mutations require healthy vector and embedding readiness.

PDF and DOCX extraction runs in a small concurrency-limited child-process
boundary with wall-clock, CPU, memory, page/span/text, and archive expansion
limits. DOCX archive entry count, entry size, encryption, and compression ratio
are validated before parsing. Plain text extraction stays in-process.

Indexing is handled by a background worker backed by persistent
`knowledgebase_index_jobs`. Jobs move through queued/running/succeeded/failed
states. Attachment rows track queued/running/indexed/failed/detached/removed
states, chunk counts, source hash, vector dimension, and diagnostics.

## Retrieval

Search uses Qdrant-native hybrid retrieval:

- a named dense vector (`dense`) populated from the configured embedding route;
- a named sparse vector (`sparse`) populated with deterministic `hashed_unicode_tokens_v2`
  token hashing and log-frequency weights;
- Qdrant `query_points` prefetches for dense + sparse candidates fused with
  Qdrant Reciprocal Rank Fusion.

This requires a Qdrant server/client that supports named dense vectors, sparse
vectors, `Prefetch`, and `FusionQuery`/RRF (the branch is developed against
`qdrant-client` 1.18.x). New collections are created with both named vector
slots. Existing legacy dense-only/unnamed collections are treated as
incompatible; Cognis does not delete them automatically. Configure a new
collection name or explicitly reset/rebuild the derived KB collection.

Metadata filters are validated against built-in, production-default, and
schema-declared filterable fields. Invalid filters return a validation error
instead of being treated as a hidden/unavailable knowledgebase. Attachment
metadata is copied into chunk and vector payload metadata during indexing, so
filterable fields can be used for normal searches. Simple `eq`/`in` filters and
array `overlap` filters are pushed to Qdrant. Other validated operators are
applied after candidate hydration with bounded overfetch.

Generic default metadata fields are filterable and included in newly created
schemas: `title`, `category`, `tags`, and canonical `source_path`. Applications
may declare additional generic fields in `metadata_schema`; Cognis does not
define source-system or domain-specific fields.

Schema-declared metadata fields support these filter type families:

- `string` and `keyword`: `eq`, `in`, and `contains`;
- `number`, `integer`, and `float`: `eq`, `gte`, `lte`, and `between`;
- `boolean`: `eq`;
- `date` and `datetime`: `eq`, `gte`, `lte`, and `between`;
- string lists: either JSON Schema form
  `{ "type": "array", "items": { "type": "string" } }` or compact
  `string[]`, with `contains` and `overlap`.

Attachment metadata accepts JSON scalar values, string arrays, and JSON
objects. Example generic schema:

```json
{
  "fields": {
    "tags": {
      "type": "array",
      "items": { "type": "string" },
      "filterable": true,
      "description": "Document tags"
    }
  }
}
```

Bulk attach supports either shared metadata for all artifacts:

```json
{
  "artifact_ids": ["art_1", "art_2"],
  "metadata": { "category": "reference" }
}
```

or per-document metadata:

```json
{
  "items": [
    {
      "artifact_id": "art_62",
      "metadata": {
        "title": "Installation guide",
        "category": "manual",
        "tags": ["installation", "reference"],
        "source_path": "docs/install.md"
      }
    }
  ]
}
```

## Chunking

Knowledgebase ingestion chunks extracted text by tokens, not raw character
counts. The defaults are intentionally conservative for broad semantic search:

- target chunk size: `800` tokens
  (`COGNIS_KNOWLEDGEBASE_CHUNK_TARGET_TOKENS`);
- overlap: `100` tokens
  (`COGNIS_KNOWLEDGEBASE_CHUNK_OVERLAP_TOKENS`);
- maximum chunks per artifact: `2000`
  (`COGNIS_KNOWLEDGEBASE_MAX_CHUNKS_PER_ARTIFACT`).

The tokenizer uses `tiktoken` with the `cl100k_base` encoding. If the tokenizer
is unavailable, the indexer falls back to a conservative character estimate.
Markdown text preserves heading context in chunks, long single spans such as PDF
pages are split by the token budget, and overlap is built from whole source spans
so locators remain honest. The max-chunks limit is a guardrail, not truncation:
if an artifact would exceed it, indexing fails with an actionable error asking
the caller to split the artifact or raise the cap.

Per-knowledgebase overrides are supported through `settings.chunking`:

```json
{
  "settings": {
    "chunking": {
      "target_tokens": 512,
      "overlap_tokens": 64,
      "max_chunks_per_artifact": 250
    }
  }
}
```

Overrides are optional and fall back to the global defaults. `target_tokens` must
be between `128` and `8192`, `overlap_tokens` must be between `0` and `2048` and
smaller than `target_tokens`, and `max_chunks_per_artifact` must be between `1`
and `100000`.

Matches include snippets, score breakdowns, metadata, and source locators with
artifact ID/hash and available line/page/paragraph/timestamp offsets. Source
context reads canonical artifact bytes through the artifact store when
available, applies requested before/after character windows, and warns if the
canonical artifact is missing or its hash changed. Agents should prefer
`knowledgebase_read_source_context` over generic artifact tools when inspecting
KB search citations or chunk IDs, because source-context authorization is scoped
to the assigned knowledgebase and does not require broad raw artifact access.

## Ownership, agent assignment, and sharing

Knowledgebases are owner-managed capabilities. Owners can create, attach,
detach, reindex, retry, and assign their own knowledgebases. Assignment is stored
on the agent permission document as `allowed_knowledgebases`, analogous to
credential capability exposure. The primary API for assignment is
`/api/v1/knowledgebases/{knowledgebase_id}/agents/{agent_id}`.

Agent runtime use is intentionally narrower than direct owner management:

- direct owner API/tool calls without an active agent context can list and use
  the owner's knowledgebases;
- active agent sessions only list/search/read knowledgebases assigned to that
  active agent, even when the actor is the owner;
- if an agent is shared, an active grantee with `use` access to that agent can
  search, Ask, and read bounded source context from indexed ranges in an
  assigned knowledgebase;
- shared-agent use does not grant attach/detach/reindex/delete/assignment
  permissions, document enumeration, full source reading/downloading, or broad
  raw artifact access.

Direct owners may enumerate/read/ingest/manage documents. Viewer accounts may
read owner-visible document state but cannot mutate it. Archived knowledgebases
remain browse/search/Ask readable while all document and settings mutations are
rejected except reactivation and terminal deletion. Unrelated and cross-owner
requests are non-disclosing 404 responses.

Owners may also create direct `view` grants under
`/{knowledgebase_id}/shares`. Direct user grantees discover the knowledgebase
with `access_level="shared"` and may get metadata, browse/read bounded document
content, Search, Ask, and read source context. They cannot mutate lifecycle,
settings, documents, jobs, agent assignment, or sharing, and never receive
arbitrary artifact URLs. Grant and revoke operations require an active
knowledgebase. Candidate discovery is owner-only, requires a trimmed query of
at least two characters, and returns at most 20 active non-system users.
Revocation takes effect on the next request. Direct grants are deliberately
ignored whenever an active agent context is present, preserving the narrower
assigned-agent boundary.

Search and source-context authorization is enforced server-side. The service
resolves the permitted knowledgebase before vector search or chunk hydration,
uses the resolved knowledgebase owner plus knowledgebase ID as mandatory vector
filters, and only returns indexed source ranges belonging to that permitted
knowledgebase.

Dense vector hits are fused by the Cognis `chunk_id` carried in the vector
payload, not by backend-specific vector point IDs, so semantic-only matches and
score breakdowns resolve back to the stored chunk and source locator.
Every search match also returns its non-null `kb_artifact_id` as the stable
Knowledge document identity. `artifact_id` remains immutable revision
provenance, while `chunk_id` remains citation and source-context identity.

`POST /api/v1/knowledgebases/{id}/facets` returns exact typed values and
document-level counts from active attachment metadata. Requests are limited to
five facetable fields and 100 values per field. Counts apply current filters
with the faceted field's own filter excluded. Array values are counted once per
document. Exact aggregation stops with a typed limitation when the active
document ceiling is exceeded; it never returns sampled or chunk-level counts.

Markdown and text documents can use a version-one YAML frontmatter envelope at
byte zero. The canonical source remains unchanged. Safe, bounded frontmatter is
removed from extracted body spans and merged with explicit attachment metadata
into generation-owned active metadata during successful generation activation.
Explicit attachment metadata remains separate and is never rewritten by
frontmatter. Pending or failed replacements retain the last-good active
metadata and facets. Malformed envelopes fail
indexing without replacing the prior generation. Arbitrary `Metadata:` headings
remain normal body text.

Relative Markdown resources can be served through
`GET /api/v1/knowledgebases/{id}/documents/{source_kb_artifact_id}/resources/{path}`.
Resolution is limited to active attachments in the same knowledgebase and is
relative to the containing document path. Missing resources are not fetched or
ingested. Each request rechecks direct browse access; assigned-agent-only access
does not grant resource bytes. Safe types can render inline, while active
HTML/SVG and unknown types are forced to download.

## Grounded Ask

`POST /api/v1/knowledgebases/{id}/ask` performs exactly one normal
knowledgebase retrieval and returns those raw matches in the same order. Every
evidence excerpt supplied to the model is the visible `snippet` in a returned
match; there is no hidden evidence or synthesis-only reranking.

Synthesis uses the configured default model route with a fixed backend prompt,
no tools, history, memory, or credentials, bounded context and
output, and a 45-second deadline. The question and evidence are encoded as
untrusted JSON data. The model must return structured JSON with an answer and
cited chunk IDs; citations are accepted only when they identify returned
matches. No-match requests skip the LLM. Timeout, provider, invalid-response,
and unsupported-citation failures return HTTP 200 with the raw matches and a
safe typed error with a correlation ID. Provider failures produce classified
structured diagnostics without question, evidence, credentials, or upstream
response bodies. Ask results are not persisted. One absolute deadline bounds readiness, retrieval, and
synthesis; retrieval/dependency timeout remains an operational 503, while
synthesis failure after successful retrieval remains an HTTP 200 typed result.

## Agent tools

When the Knowledgebase feature is enabled, assigned agents receive built-in
tools to list their available knowledgebases, search, and read bounded canonical
source context for a hit/chunk. Prefer the source-context tool for KB hits
instead of `artifact_read`, especially in shared-agent sessions where assigned
KB use is allowed but broad raw artifact access is not. Owner management is
performed through the direct owner API, outside active agent context.

The tools use authenticated runtime metadata for actor and active agent context;
they do not accept an owner override from the LLM and are not registered when
the feature is disabled. Read/search tools return structured errors for
knowledgebases that are missing, unavailable, or inaccessible instead of an
opaque `None` result.

## Current limitations

- First backend implementation is Qdrant only.
- Sparse vectors use deterministic hashed tokens rather than a corpus vocabulary.
- Advanced metadata filter operators are validated and applied after Qdrant
  candidate retrieval; only simple `eq`/`in` filters are pushed down to Qdrant.
- Document batches are bounded and intentionally do not provide automatic source
  synchronization.
