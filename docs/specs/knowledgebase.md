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

`GET /api/v1/knowledgebases/health` reports enablement state and notes such as
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

Users can create/list/get/update/delete knowledgebases, attach existing
artifacts, bulk attach artifacts, detach artifacts, inspect jobs, inspect
diagnostics, search, and read source context. A knowledgebase is owned by one
user/account. Attaching an artifact clears temporary expiry and marks it
attached so the canonical source survives indexing. Detaching does not delete
the artifact; it queues cleanup of derived chunks/vectors. Deleting a
knowledgebase soft-deletes the knowledgebase and queues cleanup for active
derived attachment indexes. Artifact deletion marks active attachments `removed`
and queues derived-index cleanup.

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

Production-default metadata fields are always filterable and are also included
in newly created knowledgebase schemas unless explicitly overridden:

- `lesson_no` (`integer`);
- `title` (`string`);
- `folder` (`string`);
- `category` (`keyword`);
- `tags` (`array` of strings);
- `youtube_id` (`keyword`);
- `source_path` (`string`);
- `source_paths` (`array` of strings).

Schema-declared metadata fields support these filter type families:

- `string` and `keyword`: `eq`, `in`, and `contains`;
- `number`, `integer`, and `float`: `eq`, `gte`, `lte`, and `between`;
- `boolean`: `eq`;
- `date` and `datetime`: `eq`, `gte`, `lte`, and `between`;
- string lists: either JSON Schema form
  `{ "type": "array", "items": { "type": "string" } }` or compact
  `string[]`, with `contains` and `overlap`.

Attachment metadata accepts JSON scalar values, including numeric values such as
`"document_count": 5`, string arrays such as `"tags": ["ming-kua"]`, and JSON
objects. Example schema for lesson tags:

```json
{
  "fields": {
    "tags": {
      "type": "array",
      "items": { "type": "string" },
      "filterable": true,
      "description": "Lesson tags"
    }
  }
}
```

Bulk attach supports either shared metadata for all artifacts:

```json
{
  "artifact_ids": ["art_1", "art_2"],
  "metadata": { "category": "mistnosti-domova" }
}
```

or per-document metadata:

```json
{
  "items": [
    {
      "artifact_id": "art_62",
      "metadata": {
        "lesson_no": 62,
        "title": "Ložnice",
        "category": "mistnosti-domova",
        "tags": ["ložnice", "kuchyň"],
        "source_paths": ["lessons/62-loznice.md"]
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
  inspect assigned knowledgebase state, including get/list-artifacts/jobs/status
  and diagnostics, search it, and read bounded source context from indexed
  ranges in that knowledgebase;
- shared-agent use does not grant attach/detach/reindex/delete/assignment
  permissions and does not grant broad raw artifact access.

Search and source-context authorization is enforced server-side. The service
resolves the permitted knowledgebase before vector search or chunk hydration,
uses the resolved knowledgebase owner plus knowledgebase ID as mandatory vector
filters, and only returns indexed source ranges belonging to that permitted
knowledgebase.

Dense vector hits are fused by the Cognis `chunk_id` carried in the vector
payload, not by backend-specific vector point IDs, so semantic-only matches and
score breakdowns resolve back to the stored chunk and source locator.

## Agent tools

When the Knowledgebase feature is enabled, agents receive built-in tools for
end-to-end operation:

- create/list/get/update/delete knowledgebases;
- attach one artifact, bulk attach artifact IDs with shared metadata, or bulk
  attach per-artifact items with distinct metadata;
- detach artifacts;
- list artifacts and indexing jobs;
- inspect status/diagnostics;
- reindex one artifact, reindex all active attachments, and retry failed or
  cancelled jobs;
- search a knowledgebase;
- read canonical source context for a hit/chunk. Prefer this source-context tool
  for KB hits instead of `artifact_read`, especially in shared-agent sessions
  where assigned KB access is allowed but broad raw artifact access is not.

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
- UI is intentionally deferred; current APIs expose the status and diagnostics
  needed for a future UI section.
