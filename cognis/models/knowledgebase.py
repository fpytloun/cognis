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
    REBUILD_KNOWLEDGEBASE = "rebuild_knowledgebase"


class KnowledgebaseModel(BaseModel):
    knowledgebase_id: str
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


class KnowledgebaseAttachRequest(BaseModel):
    artifact_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgebaseBulkAttachItem(BaseModel):
    artifact_id: str
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
    artifact_id: str | None
    status: KnowledgebaseArtifactStatus
    source_hash: str | None = None
    source_filename: str | None = None
    source_mime_type: str | None = None
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
