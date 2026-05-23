from __future__ import annotations

import pytest

from cognis.knowledgebase.chunking import KnowledgebaseChunkLimitExceeded, chunk_document
from cognis.knowledgebase.extraction import ExtractedDocument, SourceSpan
from cognis.knowledgebase.indexer import _resolve_chunking_settings
from cognis.knowledgebase.service import (
    KnowledgebaseRequestError,
    _apply_filters,
    _dense_hit_chunk_id,
    _filterable_fields,
    _has_residual_filters,
    _metadata_schema_with_defaults,
    _validate_filters,
    _validate_settings,
    _vector_filters,
)
from cognis.knowledgebase.vector import QdrantVectorBackend, VectorPoint, sparse_vector_from_text
from cognis.models.knowledgebase import KnowledgebaseFilter


class _Chunk:
    def __init__(self, chunk_id: str, artifact_id: str, metadata: dict[str, object]) -> None:
        self.chunk_id = chunk_id
        self.artifact_id = artifact_id
        self.metadata_json = metadata


class _Hit:
    def __init__(self, point_id: str, payload: dict[str, object]) -> None:
        self.point_id = point_id
        self.payload = payload


def test_chunk_ids_are_stable_chunk_ids_not_qdrant_point_ids() -> None:
    document = ExtractedDocument(
        spans=[SourceSpan(text="alpha beta", locator={"line_start": 1, "line_end": 1})],
        extraction_method="text",
    )

    chunks = chunk_document(
        document,
        artifact_id="art_1",
        artifact_hash="hash",
        chunk_id_prefix="kba_source",
    )

    assert chunks[0].locator["chunk_id"] == "kba_source_000000"
    assert chunks[0].locator["line_start"] == 1


def test_markdown_chunking_preserves_heading_context() -> None:
    document = ExtractedDocument(
        spans=[
            SourceSpan(text="# Course", locator={"line_start": 1, "line_end": 1}),
            SourceSpan(text="Intro text", locator={"line_start": 2, "line_end": 2}),
            SourceSpan(text="## Kitchen", locator={"line_start": 3, "line_end": 3}),
            SourceSpan(text="Kitchen advice", locator={"line_start": 4, "line_end": 4}),
            SourceSpan(text="## Bedroom", locator={"line_start": 5, "line_end": 5}),
            SourceSpan(text="Bedroom advice", locator={"line_start": 6, "line_end": 6}),
        ],
        extraction_method="text",
    )

    chunks = chunk_document(
        document,
        artifact_id="art_1",
        artifact_hash="hash",
        chunk_id_prefix="kba_source",
        metadata={"filename": "lesson.md", "mime_type": "text/markdown"},
        target_tokens=7,
        overlap_tokens=0,
        token_counter=lambda text: len(text.split()),
    )

    assert any("# Course\n## Kitchen\n\nKitchen advice" in chunk.text for chunk in chunks)
    assert any("# Course\n## Bedroom\n\nBedroom advice" in chunk.text for chunk in chunks)
    kitchen = next(chunk for chunk in chunks if "Kitchen advice" in chunk.text)
    assert kitchen.locator["heading_stack"] == ["# Course", "## Kitchen"]


def test_markdown_chunking_accepts_parameterized_markdown_mime_type() -> None:
    document = ExtractedDocument(
        spans=[
            SourceSpan(text="# Course", locator={"line_start": 1, "line_end": 1}),
            SourceSpan(text="Intro text", locator={"line_start": 2, "line_end": 2}),
        ],
        extraction_method="text",
    )

    chunks = chunk_document(
        document,
        artifact_id="art_1",
        artifact_hash="hash",
        chunk_id_prefix="kba_source",
        metadata={"filename": "lesson.txt", "mime_type": "text/markdown; charset=utf-8"},
        target_tokens=10,
        overlap_tokens=0,
        token_counter=lambda text: len(text.split()),
    )

    assert "# Course\n\nIntro text" in chunks[0].text
    assert chunks[0].locator["heading_stack"] == ["# Course"]


def test_chunk_overlap_uses_whole_spans_with_honest_locators() -> None:
    document = ExtractedDocument(
        spans=[
            SourceSpan(text="alpha beta", locator={"line_start": 1, "line_end": 1}),
            SourceSpan(text="gamma delta", locator={"line_start": 2, "line_end": 2}),
            SourceSpan(text="epsilon zeta", locator={"line_start": 3, "line_end": 3}),
        ],
        extraction_method="text",
    )

    chunks = chunk_document(
        document,
        artifact_id="art_1",
        artifact_hash="hash",
        chunk_id_prefix="kba_source",
        target_tokens=4,
        overlap_tokens=2,
        token_counter=lambda text: len(text.split()),
    )

    assert chunks[1].text.startswith("gamma delta\n")
    assert chunks[1].locator["line_start"] == 2


def test_long_span_is_split_by_token_budget() -> None:
    document = ExtractedDocument(
        spans=[
            SourceSpan(
                text=" ".join(f"word{i}" for i in range(12)),
                locator={"page_start": 1, "page_end": 1},
            )
        ],
        extraction_method="pdf",
    )

    chunks = chunk_document(
        document,
        artifact_id="art_1",
        artifact_hash="hash",
        chunk_id_prefix="kba_source",
        target_tokens=5,
        overlap_tokens=0,
        token_counter=lambda text: len(text.split()),
    )

    assert len(chunks) == 3
    assert all(chunk.token_count <= 5 for chunk in chunks)
    assert all(chunk.locator["page_start"] == 1 for chunk in chunks)


def test_long_unsplittable_span_is_split_by_token_budget() -> None:
    document = ExtractedDocument(
        spans=[
            SourceSpan(
                text="x" * 50,
                locator={"line_start": 1, "line_end": 1},
            )
        ],
        extraction_method="text",
    )

    chunks = chunk_document(
        document,
        artifact_id="art_1",
        artifact_hash="hash",
        chunk_id_prefix="kba_source",
        target_tokens=5,
        overlap_tokens=0,
        token_counter=lambda text: len(text),
    )

    assert len(chunks) == 10
    assert all(chunk.token_count <= 5 for chunk in chunks)
    assert all(chunk.locator["line_start"] == 1 for chunk in chunks)


def test_chunk_limit_exceeding_fails_instead_of_truncating() -> None:
    document = ExtractedDocument(
        spans=[
            SourceSpan(text="alpha beta", locator={"line_start": 1, "line_end": 1}),
            SourceSpan(text="gamma delta", locator={"line_start": 2, "line_end": 2}),
            SourceSpan(text="epsilon zeta", locator={"line_start": 3, "line_end": 3}),
        ],
        extraction_method="text",
    )

    with pytest.raises(KnowledgebaseChunkLimitExceeded, match="split it into smaller artifacts"):
        chunk_document(
            document,
            artifact_id="art_1",
            artifact_hash="hash",
            chunk_id_prefix="kba_source",
            target_tokens=2,
            overlap_tokens=0,
            max_chunks=1,
            token_counter=lambda text: len(text.split()),
        )


def test_metadata_filters_validate_and_apply_to_chunks() -> None:
    schema = {"fields": {"source_type": {"type": "string", "filterable": True}}}
    filters = [KnowledgebaseFilter(field="source_type", op="eq", value="manual")]

    _validate_filters(filters, schema)
    filtered = _apply_filters(
        [
            _Chunk("c1", "a1", {"source_type": "manual"}),
            _Chunk("c2", "a2", {"source_type": "ticket"}),
        ],  # type: ignore[list-item]
        filters,
    )

    assert [chunk.chunk_id for chunk in filtered] == ["c1"]
    assert _vector_filters(owner_email="u", knowledgebase_id="kb", filters=filters) == {
        "owner_email": "u",
        "knowledgebase_id": "kb",
        "source_type": "manual",
    }


def test_metadata_filters_accept_json_schema_string_arrays() -> None:
    schema = {
        "fields": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "filterable": True,
            }
        }
    }
    filters = [KnowledgebaseFilter(field="tags", op="overlap", value=["ming-kua"])]

    _validate_filters(filters, schema)
    filtered = _apply_filters(
        [
            _Chunk("c1", "a1", {"tags": ["ming-kua", "feng-shui"]}),
            _Chunk("c2", "a2", {"tags": ["kitchen"]}),
        ],  # type: ignore[list-item]
        filters,
    )

    assert [chunk.chunk_id for chunk in filtered] == ["c1"]


def test_production_metadata_fields_are_filterable_by_default() -> None:
    schema = _metadata_schema_with_defaults({})
    filters = [
        KnowledgebaseFilter(field="category", op="eq", value="mistnosti-domova"),
        KnowledgebaseFilter(field="lesson_no", op="eq", value=62),
        KnowledgebaseFilter(field="filename", op="eq", value="62-loznice.md"),
        KnowledgebaseFilter(field="tags", op="overlap", value=["kuchyň"]),
    ]

    _validate_filters(filters, schema)
    assert _filterable_fields({})["category"] == "keyword"
    assert _filterable_fields({})["lesson_no"] == "number"
    assert _filterable_fields({})["tags"] == "string[]"
    assert _vector_filters(owner_email="u", knowledgebase_id="kb", filters=filters) == {
        "owner_email": "u",
        "knowledgebase_id": "kb",
        "category": "mistnosti-domova",
        "lesson_no": 62,
        "filename": "62-loznice.md",
        "tags": ["kuchyň"],
    }
    assert not _has_residual_filters(filters)


def test_explicit_metadata_schema_can_override_default_field_type() -> None:
    schema = _metadata_schema_with_defaults(
        {"fields": {"lesson_no": {"type": "string", "filterable": True}}}
    )

    assert schema["fields"]["lesson_no"]["type"] == "string"
    with pytest.raises(ValueError, match="invalid for string"):
        _validate_filters([KnowledgebaseFilter(field="lesson_no", op="gte", value=10)], schema)


def test_metadata_filters_normalize_numeric_aliases_for_ranges() -> None:
    schema = {"fields": {"lesson_no": {"type": "integer", "filterable": True}}}

    _validate_filters([KnowledgebaseFilter(field="lesson_no", op="gte", value=10)], schema)
    _validate_filters(
        [KnowledgebaseFilter(field="lesson_no", op="between", value=[10, 20])], schema
    )


def test_metadata_filters_normalize_date_alias_for_ranges() -> None:
    schema = {"fields": {"published_at": {"type": "date", "filterable": True}}}

    _validate_filters(
        [
            KnowledgebaseFilter(
                field="published_at", op="between", value=["2024-01-01", "2024-12-31"]
            )
        ],
        schema,
    )


def test_metadata_filters_reject_unknown_field() -> None:
    with pytest.raises(ValueError, match="not filterable"):
        _validate_filters([KnowledgebaseFilter(field="secret", op="eq", value="x")], {})


def test_validate_settings_allows_chunking_overrides() -> None:
    settings = _validate_settings(
        {
            "chunking": {
                "target_tokens": 512,
                "overlap_tokens": 64,
                "max_chunks_per_artifact": 250,
            }
        }
    )

    assert settings["chunking"]["target_tokens"] == 512
    assert settings["chunking"]["overlap_tokens"] == 64
    assert settings["chunking"]["max_chunks_per_artifact"] == 250


def test_validate_settings_rejects_invalid_chunking_overrides() -> None:
    with pytest.raises(KnowledgebaseRequestError, match="overlap_tokens"):
        _validate_settings({"chunking": {"target_tokens": 256, "overlap_tokens": 256}})

    with pytest.raises(KnowledgebaseRequestError, match="target_tokens"):
        _validate_settings({"chunking": {"target_tokens": 64}})

    with pytest.raises(KnowledgebaseRequestError, match="max_chunks_per_artifact"):
        _validate_settings({"chunking": {"max_chunks_per_artifact": 0}})


def test_resolve_chunking_settings_prefers_kb_overrides() -> None:
    assert _resolve_chunking_settings(
        {
            "chunking": {
                "target_tokens": 512,
                "overlap_tokens": 0,
                "max_chunks_per_artifact": 250,
            }
        },
        default_max_chunks_per_artifact=2000,
        default_target_tokens=800,
        default_overlap_tokens=100,
    ) == (250, 512, 0)

    assert _resolve_chunking_settings(
        {},
        default_max_chunks_per_artifact=2000,
        default_target_tokens=800,
        default_overlap_tokens=100,
    ) == (2000, 800, 100)


def test_dense_hit_ranking_uses_payload_chunk_id_not_vector_point_id() -> None:
    assert _dense_hit_chunk_id(_Hit("uuid-point", {"chunk_id": "chunk_1"})) == "chunk_1"
    assert _dense_hit_chunk_id(_Hit("uuid-point", {})) == "uuid-point"


def test_sparse_vectors_are_deterministic_and_token_matching() -> None:
    first = sparse_vector_from_text("Alpha alpha beta!")
    second = sparse_vector_from_text("alpha beta")

    assert first == sparse_vector_from_text("Alpha alpha beta!")
    assert set(first.indices).issuperset(second.indices)
    assert len(first.indices) == 2


def test_sparse_vectors_tokenize_czech_diacritics_as_whole_terms() -> None:
    text = "Ložnice dveře schodiště koupelna Feng Shui"
    tokens = sparse_vector_from_text(text)

    assert len(tokens.indices) == 6
    assert tokens == sparse_vector_from_text(text)


def test_residual_filter_detection() -> None:
    assert not _has_residual_filters([KnowledgebaseFilter(field="artifact_id", op="eq", value="a")])
    assert _has_residual_filters(
        [KnowledgebaseFilter(field="filename", op="contains", value="guide")]
    )


def test_vector_filters_are_scoped_to_resolved_owner_and_kb() -> None:
    filters = _vector_filters(owner_email="owner@example.com", knowledgebase_id="kb_1", filters=[])

    assert filters["owner_email"] == "owner@example.com"
    assert filters["knowledgebase_id"] == "kb_1"


@pytest.mark.asyncio
async def test_qdrant_backend_uses_native_hybrid_prefetch(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("qdrant_client")
    from qdrant_client import models

    class _Client:
        def __init__(self) -> None:
            self.created: dict[str, object] | None = None
            self.upserted: list[object] | None = None
            self.query_kwargs: dict[str, object] | None = None

        async def get_collection(self, collection_name: str) -> object:
            raise Exception("not found")

        async def create_collection(self, **kwargs: object) -> None:
            self.created = kwargs

        async def upsert(self, **kwargs: object) -> None:
            self.upserted = list(kwargs["points"])  # type: ignore[index]

        async def query_points(self, **kwargs: object) -> object:
            self.query_kwargs = kwargs

            class _Hit:
                id = "point_1"
                score = 1.0
                payload = {"chunk_id": "chunk_1"}

            class _Result:
                points = [_Hit()]

            return _Result()

    client = _Client()
    backend = QdrantVectorBackend(url="http://qdrant.invalid", api_key="", collection="kb_test")
    monkeypatch.setattr(backend, "_get_client", lambda: client)

    sparse = sparse_vector_from_text("ložnice dveře")
    await backend.upsert(
        [
            VectorPoint(
                point_id="point_1",
                vector=[0.1, 0.2],
                sparse_vector=sparse,
                payload={"chunk_id": "chunk_1"},
            )
        ],
        vector_size=2,
    )
    hits = await backend.search([0.1, 0.2], sparse_vector=sparse, limit=3)

    assert client.created is not None
    assert "sparse_vectors_config" in client.created
    assert client.upserted is not None
    assert client.query_kwargs is not None
    assert isinstance(client.query_kwargs["query"], models.FusionQuery)
    assert len(client.query_kwargs["prefetch"]) == 2  # dense + native sparse prefetch
    assert hits[0].payload["chunk_id"] == "chunk_1"
