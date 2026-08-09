"""Knowledgebase API/domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class KnowledgebaseStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class KnowledgebaseArtifactStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    INDEXED = "indexed"
    STALE = "stale"
    FAILED = "failed"
    REMOVED = "removed"
    DETACHED = "detached"


class KnowledgebaseJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KnowledgebaseJobType(StrEnum):
    INDEX_ARTIFACT = "index_artifact"
    REINDEX_ARTIFACT = "reindex_artifact"
    DELETE_ARTIFACT_INDEX = "delete_artifact_index"
    DELETE_STALE_VECTORS = "delete_stale_vectors"
    REBUILD_KNOWLEDGEBASE = "rebuild_knowledgebase"


class KnowledgebaseModel(BaseModel):
    knowledgebase_id: str
    owner_email: str | None = None
    access_level: Literal["owner", "shared"] = "owner"
    name: str
    description: str | None = None
    status: KnowledgebaseStatus = KnowledgebaseStatus.ACTIVE
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None


class KnowledgebaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class KnowledgebaseUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    metadata_schema: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    status: Literal["active", "archived"] | None = None


class KnowledgebaseShareRequest(BaseModel):
    user_email: str
    permission: Literal["view"] = "view"
    note: str | None = Field(default=None, max_length=500)


class KnowledgebaseShareModel(BaseModel):
    grant_id: str
    user_email: str
    user_name: str | None = None
    permission: Literal["view"] = "view"
    granted_at: datetime
    note: str | None = None


class KnowledgebaseShareCandidate(BaseModel):
    email: str
    name: str | None = None


class KnowledgebaseAttachRequest(BaseModel):
    artifact_id: str
    source_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgebaseBulkAttachItem(BaseModel):
    artifact_id: str
    source_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgebaseBulkAttachRequest(BaseModel):
    artifact_ids: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    items: list[KnowledgebaseBulkAttachItem] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def require_artifacts(self) -> KnowledgebaseBulkAttachRequest:
        if bool(self.artifact_ids) == bool(self.items):
            raise ValueError("Provide exactly one of artifact_ids or items")
        return self


class KnowledgebaseArtifactModel(BaseModel):
    kb_artifact_id: str
    knowledgebase_id: str
    source_path: str | None = None
    artifact_id: str | None
    pending_artifact_id: str | None = None
    pending_source_hash: str | None = None
    active_generation: int = 0
    desired_generation: int = 0
    status: KnowledgebaseArtifactStatus
    source_hash: str | None = None
    source_filename: str | None = None
    source_mime_type: str | None = None
    source_size_bytes: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_count: int = 0
    last_job_id: str | None = None
    last_error: str | None = None
    last_diagnostics: dict[str, Any] = Field(default_factory=dict)
    attached_at: datetime | None = None
    indexed_at: datetime | None = None
    stale_at: datetime | None = None
    removed_at: datetime | None = None


class KnowledgebaseIndexJobModel(BaseModel):
    job_id: str
    knowledgebase_id: str
    kb_artifact_id: str | None = None
    artifact_id: str | None = None
    generation: int = 0
    job_type: KnowledgebaseJobType
    status: KnowledgebaseJobStatus
    attempts: int = 0
    error: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    chunks_indexed: int = 0
    chunks_deleted: int = 0
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class KnowledgebaseChunkLocator(BaseModel):
    artifact_id: str
    artifact_hash: str | None = None
    chunk_id: str
    chunk_index: int
    char_start: int | None = None
    char_end: int | None = None
    byte_start: int | None = None
    byte_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    paragraph_start: int | None = None
    paragraph_end: int | None = None
    timestamp_start_ms: int | None = None
    timestamp_end_ms: int | None = None
    extraction_method: str


class KnowledgebaseSourceCitation(BaseModel):
    artifact_id: str
    filename: str | None = None
    mime_type: str | None = None
    locator: KnowledgebaseChunkLocator


class KnowledgebaseFilter(BaseModel):
    field: str
    op: Literal["eq", "in", "contains", "overlap", "gte", "lte", "between"]
    value: Any


class KnowledgebaseSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    filters: list[KnowledgebaseFilter] = Field(default_factory=list)


class KnowledgebaseSearchMatch(BaseModel):
    chunk_id: str
    kb_artifact_id: str
    artifact_id: str
    snippet: str
    score: float
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    citation: KnowledgebaseSourceCitation


class KnowledgebaseSearchResponse(BaseModel):
    matches: list[KnowledgebaseSearchMatch]
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class KnowledgebaseHealth(BaseModel):
    enabled: bool
    vector_backend: str
    embedding_route_configured: bool
    healthy: bool = False
    notes: list[str] = Field(default_factory=list)


class KnowledgebaseCapabilities(BaseModel):
    enabled: bool
    vector_backend: str
    backend_ready: bool
    embedding_ready: bool
    indexer_ready: bool
    ask_ready: bool
    supported_mime_types: list[str] = Field(default_factory=list)
    supported_extensions: list[str] = Field(default_factory=list)
    limits: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class KnowledgebaseDocumentListResponse(BaseModel):
    documents: list[KnowledgebaseArtifactModel]
    next_cursor: str | None = None


class KnowledgebaseDocumentDetail(KnowledgebaseArtifactModel):
    last_job: KnowledgebaseIndexJobModel | None = None


class KnowledgebaseDocumentContent(BaseModel):
    kb_artifact_id: str
    artifact_id: str
    source_path: str | None = None
    content_mode: Literal["source", "extracted"]
    mime_type: str
    text: str
    size_bytes: int
    extraction_method: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class KnowledgebaseDocumentUpdateRequest(BaseModel):
    source_path: str | None = None
    metadata: dict[str, Any] | None = None


class KnowledgebaseIngestOutcome(BaseModel):
    filename: str
    source_path: str | None = None
    status: Literal["created", "updated", "unchanged", "skipped", "failed"]
    artifact_id: str | None = None
    kb_artifact_id: str | None = None
    job_id: str | None = None
    error_code: str | None = None
    message: str | None = None


class KnowledgebaseIngestResponse(BaseModel):
    outcomes: list[KnowledgebaseIngestOutcome]


class KnowledgebaseAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    filters: list[KnowledgebaseFilter] = Field(default_factory=list)
    limit: int = Field(default=8, ge=1, le=20)
    max_answer_tokens: int = Field(default=700, ge=64, le=2000)


class KnowledgebaseAskError(BaseModel):
    code: Literal[
        "synthesis_timeout",
        "provider_error",
        "invalid_response",
        "unsupported_citation",
    ]
    message: str
    correlation_id: str


class KnowledgebaseFacetRequest(BaseModel):
    fields: list[str] = Field(min_length=1, max_length=5)
    filters: list[KnowledgebaseFilter] = Field(default_factory=list)
    search: dict[str, str] = Field(default_factory=dict)
    limit_per_field: int = Field(default=20, ge=1, le=100)


class KnowledgebaseFacetValue(BaseModel):
    value: str | int | float | bool
    count: int = Field(ge=1)


class KnowledgebaseFacetField(BaseModel):
    field: str
    type: Literal["string", "number", "boolean", "datetime", "array"]
    cardinality: int = Field(ge=0)
    truncated: bool = False
    values: list[KnowledgebaseFacetValue] = Field(default_factory=list)


class KnowledgebaseFacetResponse(BaseModel):
    fields: list[KnowledgebaseFacetField]
    documents_scanned: int = Field(ge=0)


class KnowledgebaseAskResponse(BaseModel):
    status: Literal["answered", "insufficient_evidence", "error"]
    answer: str | None = None
    cited_chunk_ids: list[str] = Field(default_factory=list)
    matches: list[KnowledgebaseSearchMatch] = Field(default_factory=list)
    error: KnowledgebaseAskError | None = None


class KnowledgebaseDiagnostics(BaseModel):
    enabled: bool
    artifact_counts: dict[str, int] = Field(default_factory=dict)
    job_counts: dict[str, int] = Field(default_factory=dict)
    chunk_count: int = 0
    backend_health: dict[str, Any] = Field(default_factory=dict)


class KnowledgebaseSourceContextRequest(BaseModel):
    chunk_id: str
    before_chars: int = Field(default=500, ge=0, le=5000)
    after_chars: int = Field(default=500, ge=0, le=5000)


class KnowledgebaseSourceContextResponse(BaseModel):
    chunk_id: str
    artifact_id: str
    text: str
    locator: KnowledgebaseChunkLocator
    warnings: list[str] = Field(default_factory=list)
