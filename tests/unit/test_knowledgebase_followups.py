from __future__ import annotations

import anyio
import pytest

from cognis.knowledgebase.extraction import extract_artifact_bytes
from cognis.knowledgebase.service import KnowledgebaseRequestError, resolve_resource_source_path
from cognis.knowledgebase.vector import VectorSearchHit
from cognis.store.models import (
    ArtifactRecordRow,
    KnowledgebaseArtifactRow,
    KnowledgebaseChunkRow,
    KnowledgebaseRow,
)
from tests.unit.test_knowledgebase_routes import _client


def _seed_followup_documents(client: object) -> None:
    async def seed() -> None:
        store = client.app.state.artifact_store
        await store.async_save(
            "test",
            "artifact_pdf",
            "lesson.pdf",
            b"%PDF-1.4\nresource",
            "application/pdf",
            owner_email="owner@example.com",
        )
        await store.async_save(
            "test",
            "artifact_html",
            "unsafe.html",
            b"<script>alert(1)</script>",
            "text/html",
            owner_email="owner@example.com",
        )
        await store.async_save(
            "test",
            "artifact_svg",
            "unsafe.svg",
            b"<svg><script>alert(1)</script></svg>",
            "image/svg+xml",
            owner_email="owner@example.com",
        )
        async with client.app.state.session_factory() as session:
            kb = await session.get(KnowledgebaseRow, "kb_owner")
            assert kb is not None
            kb.metadata_schema = {
                "fields": {
                    "category": {
                        "type": "keyword",
                        "filterable": True,
                        "facetable": True,
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "filterable": True,
                        "facetable": True,
                    },
                    "score": {
                        "type": "number",
                        "filterable": True,
                        "facetable": True,
                    },
                    "event_at": {
                        "type": "datetime",
                        "filterable": True,
                    },
                }
            }
            source = await session.get(KnowledgebaseArtifactRow, "kba_1")
            assert source is not None
            source.source_path = "docs/source.md"
            source.active_generation = 1
            source.metadata_json = {"category": "lesson", "tags": ["a", "x"]}
            source.active_metadata_json = {
                "category": "lesson",
                "tags": ["a", "x"],
                "score": 5,
                "event_at": "2026-01-01T00:00:00+00:00",
            }
            session.add_all(
                [
                    ArtifactRecordRow(
                        artifact_id="artifact_pdf",
                        namespace="test",
                        object_id="artifact_pdf",
                        filename="lesson.pdf",
                        owner_email="owner@example.com",
                        mime_type="application/pdf",
                        kind="pdf",
                        purpose="knowledgebase_document",
                        size_bytes=21,
                    ),
                    ArtifactRecordRow(
                        artifact_id="artifact_html",
                        namespace="test",
                        object_id="artifact_html",
                        filename="unsafe.html",
                        owner_email="owner@example.com",
                        mime_type="text/html",
                        kind="file",
                        purpose="knowledgebase_document",
                        size_bytes=25,
                    ),
                    ArtifactRecordRow(
                        artifact_id="artifact_svg",
                        namespace="test",
                        object_id="artifact_svg",
                        filename="unsafe.svg",
                        owner_email="owner@example.com",
                        mime_type="image/svg+xml",
                        kind="file",
                        purpose="knowledgebase_document",
                        size_bytes=36,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgebaseArtifactRow(
                        kb_artifact_id="kba_pdf",
                        knowledgebase_id="kb_owner",
                        artifact_id="artifact_pdf",
                        source_path="docs/lesson.pdf",
                        status="indexed",
                        active_generation=1,
                        metadata_json={"category": "lesson", "tags": ["b", "x"]},
                        active_metadata_json={
                            "category": "lesson",
                            "tags": ["b", "x"],
                            "score": 10,
                            "event_at": "2026-02-01T00:00:00+00:00",
                        },
                    ),
                    KnowledgebaseArtifactRow(
                        kb_artifact_id="kba_html",
                        knowledgebase_id="kb_owner",
                        artifact_id="artifact_html",
                        source_path="docs/unsafe.html",
                        status="indexed",
                        active_generation=1,
                        metadata_json={"category": "guide", "tags": ["a"]},
                        active_metadata_json={
                            "category": "guide",
                            "tags": ["a"],
                            "score": 20,
                            "event_at": "2026-03-01T00:00:00+00:00",
                        },
                    ),
                    KnowledgebaseArtifactRow(
                        kb_artifact_id="kba_svg",
                        knowledgebase_id="kb_owner",
                        artifact_id="artifact_svg",
                        source_path="docs/unsafe.svg",
                        status="indexed",
                        active_generation=1,
                        metadata_json={"category": "guide", "tags": ["a"]},
                        active_metadata_json={
                            "category": "guide",
                            "tags": ["a"],
                            "score": 30,
                            "event_at": "2026-04-01T00:00:00+00:00",
                        },
                    ),
                    KnowledgebaseArtifactRow(
                        kb_artifact_id="kba_pending_first",
                        knowledgebase_id="kb_owner",
                        source_path="docs/pending.md",
                        status="queued",
                        metadata_json={"category": "pending"},
                        active_metadata_json={"category": "pending"},
                    ),
                    KnowledgebaseArtifactRow(
                        kb_artifact_id="kba_failed_first",
                        knowledgebase_id="kb_owner",
                        source_path="docs/failed.md",
                        status="failed",
                        metadata_json={"category": "failed"},
                        active_metadata_json={"category": "failed"},
                    ),
                ]
            )
            await session.commit()

    anyio.run(seed)


def test_resource_route_is_manifest_scoped_authorized_and_content_safe(tmp_path: object) -> None:
    client = _client(tmp_path)
    _seed_followup_documents(client)
    base = "/api/v1/knowledgebases/kb_owner/documents/kba_1/resources"

    pdf = client.get(f"{base}/lesson.pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.headers["content-disposition"].startswith("inline")
    assert pdf.headers["x-content-type-options"] == "nosniff"
    assert pdf.headers["cache-control"] == "private, no-store"

    html = client.get(f"{base}/unsafe.html")
    assert html.status_code == 200
    assert html.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in html.headers["content-security-policy"]
    svg = client.get(f"{base}/unsafe.svg")
    assert svg.status_code == 200
    assert svg.headers["content-disposition"].startswith("attachment")

    for bad_path in (
        "../lesson.pdf",
        "%2e%2e%2flesson.pdf",
        "%252e%252e%252flesson.pdf",
        "https:%2F%2Fevil.example%2Fx",
        "missing.pdf",
    ):
        assert client.get(f"{base}/{bad_path}").status_code == 404

    unrelated = {"x-user": "other@example.com"}
    assert client.get(f"{base}/lesson.pdf", headers=unrelated).status_code == 404
    agent_only = {
        "x-user": "grantee@example.com",
        "x-agent-id": "agent_owner",
        "x-agent-owner": "owner@example.com",
    }
    assert client.get(f"{base}/lesson.pdf", headers=agent_only).status_code == 404
    assert (
        client.get(
            "/api/v1/knowledgebases/kb_owner/documents/guessed/resources/lesson.pdf"
        ).status_code
        == 404
    )

    assert (
        client.put(
            "/api/v1/knowledgebases/kb_owner/shares",
            json={"user_email": "grantee@example.com"},
        ).status_code
        == 200
    )
    shared = {"x-user": "grantee@example.com"}
    assert client.get(f"{base}/lesson.pdf", headers=shared).status_code == 200
    assert (
        client.delete("/api/v1/knowledgebases/kb_owner/shares/grantee@example.com").status_code
        == 200
    )
    assert client.get(f"{base}/lesson.pdf", headers=shared).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute.pdf",
        r"folder\file.pdf",
        "../escape.pdf",
        "folder/./file.pdf",
        "%2e%2e/file.pdf",
        "%252e%252e/file.pdf",
        "https://evil.example/file.pdf",
        "folder//file.pdf",
        "bad\x00file.pdf",
    ],
)
def test_resource_path_normalization_rejects_ambiguous_or_traversing_paths(path: str) -> None:
    with pytest.raises(KnowledgebaseRequestError):
        resolve_resource_source_path("docs/source.md", path)


def test_resource_root_alias_is_explicit_and_kb_relative() -> None:
    assert (
        resolve_resource_source_path("docs/source.md", "knowledge/resources/reference/lesson.pdf")
        == "reference/lesson.pdf"
    )


def test_facets_are_exact_document_level_filter_aware_and_bounded(tmp_path: object) -> None:
    client = _client(tmp_path)
    _seed_followup_documents(client)
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/facets",
        json={
            "fields": ["category", "tags"],
            "filters": [{"field": "category", "op": "in", "value": ["lesson"]}],
            "search": {"category": "le"},
            "limit_per_field": 20,
        },
    )
    assert response.status_code == 200
    fields = {field["field"]: field for field in response.json()["fields"]}
    assert fields["category"]["values"] == [{"value": "lesson", "count": 2}]
    assert fields["tags"]["values"] == [
        {"value": "x", "count": 2},
        {"value": "a", "count": 1},
        {"value": "b", "count": 1},
    ]
    assert response.json()["documents_scanned"] == 4

    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/facets",
            json={"fields": ["title"], "limit_per_field": 20},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/facets",
            json={"fields": ["category"] * 2},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/facets",
            json={"fields": ["category"]},
            headers={"x-user": "other@example.com"},
        ).status_code
        == 404
    )


def test_facets_return_typed_limit_instead_of_inexact_counts(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.knowledgebase import service as service_module

    client = _client(tmp_path)
    _seed_followup_documents(client)
    monkeypatch.setattr(service_module, "_MAX_FACET_DOCUMENTS", 2)
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/facets",
        json={"fields": ["category"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "facet_document_limit"


@pytest.mark.parametrize(
    "filters",
    [
        [{"field": "score", "op": "gte", "value": "10"}],
        [{"field": "score", "op": "between", "value": ["5", "20"]}],
    ],
)
def test_numeric_filter_strings_are_safely_coerced(
    tmp_path: object, filters: list[dict[str, object]]
) -> None:
    client = _client(tmp_path)
    _seed_followup_documents(client)
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/facets",
        json={"fields": ["category"], "filters": filters},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "filters",
    [
        [{"field": "category", "op": "eq", "value": ["lesson"]}],
        [{"field": "event_at", "op": "gte", "value": "not-a-date"}],
        [{"field": "score", "op": "gte", "value": "not-a-number"}],
    ],
)
def test_invalid_typed_filters_return_validation_error(
    tmp_path: object, filters: list[dict[str, object]]
) -> None:
    client = _client(tmp_path)
    _seed_followup_documents(client)
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/facets",
        json={"fields": ["category"], "filters": filters},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation_error"


def test_datetime_equality_uses_residual_filter_for_equivalent_offsets(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)

    async def seed() -> None:
        async with client.app.state.session_factory() as session:
            kb = await session.get(KnowledgebaseRow, "kb_owner")
            attachment = await session.get(KnowledgebaseArtifactRow, "kba_1")
            assert kb is not None and attachment is not None
            kb.metadata_schema = {
                "fields": {
                    "event_at": {
                        "type": "datetime",
                        "filterable": True,
                    }
                }
            }
            attachment.active_generation = 1
            session.add(
                KnowledgebaseChunkRow(
                    chunk_id="chunk_datetime",
                    knowledgebase_id="kb_owner",
                    kb_artifact_id="kba_1",
                    artifact_id="artifact_1",
                    generation=1,
                    chunk_index=0,
                    text="matching evidence",
                    text_hash="datetime-hash",
                    locator={
                        "artifact_id": "artifact_1",
                        "chunk_id": "chunk_datetime",
                        "chunk_index": 0,
                        "extraction_method": "text",
                    },
                    metadata_json={"event_at": "2026-01-01T01:00:00+01:00"},
                    vector_id="vector-datetime",
                )
            )
            await session.commit()

    anyio.run(seed)

    class _SearchVector:
        captured_filters: dict[str, object] = {}

        async def health(self) -> dict[str, object]:
            return {"ok": True}

        async def search(self, *_args: object, **kwargs: object) -> list[VectorSearchHit]:
            self.captured_filters = dict(kwargs["filters"])
            return [
                VectorSearchHit(
                    point_id="vector-datetime",
                    score=0.9,
                    payload={"chunk_id": "chunk_datetime"},
                )
            ]

    vector = _SearchVector()
    client.app.state.knowledgebase_service._vector_backend = vector
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/search",
        json={
            "query": "matching",
            "filters": [
                {
                    "field": "event_at",
                    "op": "eq",
                    "value": "2026-01-01T00:00:00Z",
                }
            ],
        },
    )
    assert response.status_code == 200
    assert [match["chunk_id"] for match in response.json()["matches"]] == ["chunk_datetime"]
    assert "event_at" not in vector.captured_filters
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/search",
        json={
            "query": "matching",
            "filters": [
                {
                    "field": "event_at",
                    "op": "in",
                    "value": [
                        "2025-12-31T23:00:00-01:00",
                        "2027-01-01T00:00:00Z",
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert [match["chunk_id"] for match in response.json()["matches"]] == ["chunk_datetime"]
    assert "event_at" not in vector.captured_filters


def test_facet_filters_use_filterable_non_facetable_datetime_type(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    _seed_followup_documents(client)
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/facets",
        json={
            "fields": ["category"],
            "filters": [
                {
                    "field": "event_at",
                    "op": "between",
                    "value": [
                        "2025-12-31T19:00:00-05:00",
                        "2026-02-01T01:00:00+01:00",
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["fields"][0]["values"] == [{"value": "lesson", "count": 2}]


def test_frontmatter_source_context_uses_body_relative_locator_space(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    content = b"---\ntitle: Guide\n---\n# First\nfirst body context\n# Later\nlater body context\n"
    extracted = extract_artifact_bytes(content, filename="context.md", mime_type="text/markdown")
    first_span = extracted.spans[1]
    later_span = extracted.spans[3]

    async def seed() -> None:
        await client.app.state.artifact_store.async_save(
            "test",
            "artifact_context",
            "context.md",
            content,
            "text/markdown",
            owner_email="owner@example.com",
        )
        async with client.app.state.session_factory() as session:
            session.add(
                ArtifactRecordRow(
                    artifact_id="artifact_context",
                    namespace="test",
                    object_id="artifact_context",
                    filename="context.md",
                    owner_email="owner@example.com",
                    mime_type="text/markdown",
                    kind="file",
                    purpose="knowledgebase_document",
                    size_bytes=len(content),
                )
            )
            await session.flush()
            session.add(
                KnowledgebaseArtifactRow(
                    kb_artifact_id="kba_context",
                    knowledgebase_id="kb_owner",
                    artifact_id="artifact_context",
                    source_path="docs/context.md",
                    status="indexed",
                    active_generation=1,
                    desired_generation=1,
                    active_metadata_json={"title": "Guide"},
                )
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgebaseChunkRow(
                        chunk_id="chunk_context_first",
                        knowledgebase_id="kb_owner",
                        kb_artifact_id="kba_context",
                        artifact_id="artifact_context",
                        generation=1,
                        chunk_index=0,
                        text=first_span.text,
                        text_hash="first-hash",
                        locator={
                            **first_span.locator,
                            "artifact_id": "artifact_context",
                            "chunk_id": "chunk_context_first",
                            "chunk_index": 0,
                            "extraction_method": "markdown_frontmatter_v1",
                        },
                        metadata_json={"title": "Guide"},
                    ),
                    KnowledgebaseChunkRow(
                        chunk_id="chunk_context_later",
                        knowledgebase_id="kb_owner",
                        kb_artifact_id="kba_context",
                        artifact_id="artifact_context",
                        generation=1,
                        chunk_index=1,
                        text=later_span.text,
                        text_hash="later-hash",
                        locator={
                            **later_span.locator,
                            "artifact_id": "artifact_context",
                            "chunk_id": "chunk_context_later",
                            "chunk_index": 1,
                            "extraction_method": "markdown_frontmatter_v1",
                        },
                        metadata_json={"title": "Guide"},
                    ),
                ]
            )
            await session.commit()

    anyio.run(seed)
    first = client.post(
        "/api/v1/knowledgebases/kb_owner/source-context",
        json={"chunk_id": "chunk_context_first", "before_chars": 8, "after_chars": 8},
    )
    later = client.post(
        "/api/v1/knowledgebases/kb_owner/source-context",
        json={"chunk_id": "chunk_context_later", "before_chars": 8, "after_chars": 8},
    )
    assert first.status_code == 200
    assert later.status_code == 200
    assert "first body context" in first.json()["text"]
    assert "later body context" in later.json()["text"]
    assert "title: Guide" not in first.json()["text"]
    assert "title: Guide" not in later.json()["text"]
    assert "source_locator_unresolved" not in first.json()["warnings"]
    assert "source_locator_unresolved" not in later.json()["warnings"]


def test_extracted_markdown_content_excludes_frontmatter_but_source_preserves_it(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    uploaded = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[
            (
                "files[]",
                (
                    "frontmatter.md",
                    b"---\ntitle: Guide\n---\n# Body\nText",
                    "text/markdown",
                ),
            )
        ],
        data={"paths[]": "docs/frontmatter.md"},
    )
    assert uploaded.status_code == 200
    document_id = uploaded.json()["outcomes"][0]["kb_artifact_id"]
    extracted = client.get(
        f"/api/v1/knowledgebases/kb_owner/documents/{document_id}/content",
        params={"content_mode": "extracted"},
    )
    assert extracted.status_code == 200
    assert extracted.json()["text"] == "# Body\nText"
    source = client.get(
        f"/api/v1/knowledgebases/kb_owner/documents/{document_id}/content",
        params={"content_mode": "source"},
    )
    assert source.status_code == 200
    assert source.json()["text"].startswith("---\ntitle: Guide")
