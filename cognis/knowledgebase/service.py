"""High-level knowledgebase service and hybrid retrieval."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.knowledgebase.access import (
    KnowledgebaseAccessContext,
    list_available_knowledgebases,
    resolve_knowledgebase_access,
)
from cognis.knowledgebase.extraction import extract_artifact_bytes
from cognis.knowledgebase.vector import SPARSE_ALGORITHM, sparse_vector_from_text
from cognis.models.knowledgebase import (
    KnowledgebaseArtifactModel,
    KnowledgebaseDiagnostics,
    KnowledgebaseFilter,
    KnowledgebaseHealth,
    KnowledgebaseIndexJobModel,
    KnowledgebaseModel,
    KnowledgebaseSearchMatch,
    KnowledgebaseSearchRequest,
    KnowledgebaseSearchResponse,
    KnowledgebaseSourceCitation,
    KnowledgebaseSourceContextResponse,
    KnowledgebaseUpdateRequest,
)
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.store.models import (
    KnowledgebaseArtifactRow,
    KnowledgebaseChunkRow,
    KnowledgebaseIndexJobRow,
    KnowledgebaseRow,
)
from cognis.store.queries import (
    assign_knowledgebase_to_agent,
    attach_artifact_to_knowledgebase,
    create_knowledgebase,
    delete_knowledgebase,
    detach_knowledgebase_artifact,
    enqueue_knowledgebase_artifact_reindex,
    enqueue_knowledgebase_reindex,
    enqueue_retry_knowledgebase_job,
    get_artifact_record,
    get_knowledgebase_chunk,
    get_model_routing,
    list_knowledgebase_artifacts,
    list_knowledgebase_chunks,
    list_knowledgebase_jobs,
    update_knowledgebase,
)


def kb_model(row: KnowledgebaseRow) -> KnowledgebaseModel:
    return KnowledgebaseModel(
        knowledgebase_id=row.knowledgebase_id,
        name=row.name,
        description=row.description,
        status=row.status,
        metadata_schema=row.metadata_schema or {},
        settings=row.settings or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


def kb_artifact_model(row: KnowledgebaseArtifactRow) -> KnowledgebaseArtifactModel:
    return KnowledgebaseArtifactModel(
        kb_artifact_id=row.kb_artifact_id,
        knowledgebase_id=row.knowledgebase_id,
        artifact_id=row.artifact_id,
        status=row.status,
        source_hash=row.source_hash,
        source_filename=row.source_filename,
        source_mime_type=row.source_mime_type,
        metadata=row.metadata_json or {},
        chunk_count=row.chunk_count,
        last_job_id=row.last_job_id,
        last_error=row.last_error,
        last_diagnostics=row.last_diagnostics or {},
        attached_at=row.attached_at,
        indexed_at=row.indexed_at,
        stale_at=row.stale_at,
        removed_at=row.removed_at,
    )


def kb_job_model(row: KnowledgebaseIndexJobRow) -> KnowledgebaseIndexJobModel:
    return KnowledgebaseIndexJobModel(
        job_id=row.job_id,
        knowledgebase_id=row.knowledgebase_id,
        kb_artifact_id=row.kb_artifact_id,
        artifact_id=row.artifact_id,
        job_type=row.job_type,
        status=row.status,
        attempts=row.attempts,
        error=row.error,
        diagnostics=row.diagnostics or {},
        chunks_indexed=row.chunks_indexed,
        chunks_deleted=row.chunks_deleted,
        queued_at=row.queued_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


_BUILTIN_FILTER_FIELDS: dict[str, str] = {
    "artifact_id": "string",
    "filename": "string",
    "mime_type": "string",
    "kind": "string",
    "purpose": "string",
    "created_at": "datetime",
}
_PRODUCTION_METADATA_FIELDS: dict[str, dict[str, Any]] = {
    "lesson_no": {
        "type": "integer",
        "filterable": True,
        "display": True,
        "description": "Source lesson number.",
    },
    "title": {
        "type": "string",
        "filterable": True,
        "display": True,
        "description": "Source document title.",
    },
    "folder": {
        "type": "string",
        "filterable": True,
        "display": True,
        "description": "Source folder or collection path.",
    },
    "category": {
        "type": "keyword",
        "filterable": True,
        "display": True,
        "description": "Source document category.",
    },
    "tags": {
        "type": "array",
        "items": {"type": "string"},
        "filterable": True,
        "display": True,
        "description": "Source document tags.",
    },
    "youtube_id": {
        "type": "keyword",
        "filterable": True,
        "display": True,
        "description": "Source YouTube video id.",
    },
    "source_path": {
        "type": "string",
        "filterable": True,
        "display": True,
        "description": "Primary source path.",
    },
    "source_paths": {
        "type": "array",
        "items": {"type": "string"},
        "filterable": True,
        "display": True,
        "description": "Source paths used to build the document.",
    },
}
_OPS_BY_TYPE: dict[str, set[str]] = {
    "string": {"eq", "in", "contains"},
    "keyword": {"eq", "in", "contains"},
    "number": {"eq", "gte", "lte", "between"},
    "datetime": {"eq", "gte", "lte", "between"},
    "boolean": {"eq"},
    "string[]": {"contains", "overlap"},
}
_CHUNKING_SETTINGS_MIN = {
    "target_tokens": 128,
    "overlap_tokens": 0,
    "max_chunks_per_artifact": 1,
}
_CHUNKING_SETTINGS_MAX = {
    "target_tokens": 8192,
    "overlap_tokens": 2048,
    "max_chunks_per_artifact": 100_000,
}


def _normalize_metadata_field_type(spec: dict[str, Any]) -> str | None:
    raw_type = str(spec.get("type") or "string").lower()
    if raw_type in {"string", "keyword", "boolean"}:
        return raw_type
    if raw_type in {"integer", "number", "float"}:
        return "number"
    if raw_type in {"date", "datetime"}:
        return "datetime"
    if raw_type in {"string[]", "list[string]", "array[string]"}:
        return "string[]"
    if raw_type == "array":
        items = spec.get("items")
        if isinstance(items, dict) and str(items.get("type") or "").lower() == "string":
            return "string[]"
        return None
    return None


def _filterable_fields(metadata_schema: dict[str, Any] | None) -> dict[str, str]:
    fields = dict(_BUILTIN_FILTER_FIELDS)
    for name, spec in _PRODUCTION_METADATA_FIELDS.items():
        field_type = _normalize_metadata_field_type(spec)
        if field_type is not None:
            fields[name] = field_type
    for name, spec in ((metadata_schema or {}).get("fields") or {}).items():
        if isinstance(spec, dict) and spec.get("filterable") is True:
            field_type = _normalize_metadata_field_type(spec)
            if field_type is not None:
                fields[str(name)] = field_type
    return fields


def _metadata_schema_with_defaults(metadata_schema: dict[str, Any] | None) -> dict[str, Any]:
    schema = dict(metadata_schema or {})
    fields = dict(schema.get("fields") or {})
    for name, spec in _PRODUCTION_METADATA_FIELDS.items():
        fields.setdefault(name, dict(spec))
    schema["fields"] = fields
    return schema


def _validate_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    if not settings:
        return {}
    if not isinstance(settings, dict):
        raise KnowledgebaseRequestError("settings must be an object")
    normalized = dict(settings)
    chunking = normalized.get("chunking")
    if chunking is None:
        return normalized
    if not isinstance(chunking, dict):
        raise KnowledgebaseRequestError("settings.chunking must be an object")
    chunking = dict(chunking)
    for field, minimum in _CHUNKING_SETTINGS_MIN.items():
        if field not in chunking:
            continue
        value = chunking[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise KnowledgebaseRequestError(f"settings.chunking.{field} must be an integer")
        maximum = _CHUNKING_SETTINGS_MAX[field]
        if value < minimum or value > maximum:
            raise KnowledgebaseRequestError(
                f"settings.chunking.{field} must be between {minimum} and {maximum}"
            )
    target_tokens = chunking.get("target_tokens")
    overlap_tokens = chunking.get("overlap_tokens")
    if (
        isinstance(target_tokens, int)
        and not isinstance(target_tokens, bool)
        and isinstance(overlap_tokens, int)
        and not isinstance(overlap_tokens, bool)
        and overlap_tokens >= target_tokens
    ):
        raise KnowledgebaseRequestError(
            "settings.chunking.overlap_tokens must be smaller than target_tokens"
        )
    normalized["chunking"] = chunking
    return normalized


def _validate_filters(
    filters: list[KnowledgebaseFilter], metadata_schema: dict[str, Any] | None
) -> None:
    allowed = _filterable_fields(metadata_schema)
    for item in filters:
        field_type = allowed.get(item.field)
        if field_type is None:
            raise ValueError(f"metadata filter field is not filterable: {item.field}")
        if item.op not in _OPS_BY_TYPE[field_type]:
            raise ValueError(f"metadata filter operator {item.op!r} is invalid for {field_type}")
        if item.op == "between" and (
            not isinstance(item.value, list | tuple) or len(item.value) != 2
        ):
            raise ValueError("metadata filter operator 'between' requires a two-item value")
        if item.op in {"in", "overlap"} and not isinstance(item.value, list):
            raise ValueError(f"metadata filter operator {item.op!r} requires a list value")


def _metadata_value(chunk: KnowledgebaseChunkRow, field: str) -> Any:
    if field == "artifact_id":
        return chunk.artifact_id
    metadata = chunk.metadata_json or {}
    return metadata.get(field)


def _filter_matches_value(actual: Any, item: KnowledgebaseFilter) -> bool:
    if item.op == "eq":
        return actual == item.value
    if item.op == "in":
        return actual in item.value
    if item.op == "contains":
        if isinstance(actual, list):
            return item.value in actual
        return str(item.value).lower() in str(actual or "").lower()
    if item.op == "overlap":
        return (
            bool(set(actual or []).intersection(set(item.value)))
            if isinstance(actual, list)
            else False
        )
    if item.op == "gte":
        return actual is not None and actual >= item.value
    if item.op == "lte":
        return actual is not None and actual <= item.value
    if item.op == "between":
        low, high = item.value
        return actual is not None and low <= actual <= high
    return False


def _apply_filters(
    chunks: list[KnowledgebaseChunkRow], filters: list[KnowledgebaseFilter]
) -> list[KnowledgebaseChunkRow]:
    if not filters:
        return chunks
    return [
        chunk
        for chunk in chunks
        if all(_filter_matches_value(_metadata_value(chunk, item.field), item) for item in filters)
    ]


def _vector_filters(
    *, owner_email: str, knowledgebase_id: str, filters: list[KnowledgebaseFilter]
) -> dict[str, Any]:
    result: dict[str, Any] = {"owner_email": owner_email, "knowledgebase_id": knowledgebase_id}
    for item in filters:
        if item.op in {"eq", "in", "overlap"}:
            result[item.field] = item.value
    return result


def _has_residual_filters(filters: list[KnowledgebaseFilter]) -> bool:
    return any(item.op not in {"eq", "in", "overlap"} for item in filters)


class KnowledgebaseValidationError(RuntimeError):
    """Raised when a knowledgebase request is syntactically valid but semantically invalid."""


class KnowledgebaseRequestError(RuntimeError):
    """Raised when a knowledgebase management request contains invalid values."""


def _dense_hit_chunk_id(hit: Any) -> str:
    payload = getattr(hit, "payload", None) or {}
    return str(payload.get("chunk_id") or hit.point_id)


class KnowledgebaseService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        artifact_store: Any | None = None,
        llm: Any,
        vector_backend: Any,
        enabled: bool,
        disabled_notes: list[str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_store = artifact_store
        self._llm = llm
        self._vector_backend = vector_backend
        self.enabled = enabled
        self.disabled_notes = disabled_notes or []

    async def health(self) -> KnowledgebaseHealth:
        backend_health = await self._vector_backend.health()
        async with self._session_factory() as session:
            route = await get_model_routing(session, "embedding", SYSTEM_USER_EMAIL)
        notes = list(self.disabled_notes)
        if route is None:
            notes.append("embedding route not configured")
        return KnowledgebaseHealth(
            enabled=self.enabled,
            vector_backend=getattr(self._vector_backend, "name", "unknown"),
            embedding_route_configured=route is not None,
            healthy=self.enabled and bool(backend_health.get("ok", False)),
            notes=notes,
        )

    def require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError("; ".join(self.disabled_notes) or "knowledgebase feature disabled")

    async def create(
        self,
        *,
        owner_email: str,
        name: str,
        description: str | None,
        metadata_schema: dict[str, Any],
        settings: dict[str, Any],
        access_context: KnowledgebaseAccessContext | None = None,
    ) -> KnowledgebaseModel:
        self.require_enabled()
        async with self._session_factory() as session:
            row = await create_knowledgebase(
                session,
                owner_email=owner_email,
                name=name,
                description=description,
                metadata_schema=_metadata_schema_with_defaults(metadata_schema),
                settings=_validate_settings(settings),
            )
            if (
                access_context is not None
                and access_context.agent_id
                and (
                    access_context.agent_owner_email is None
                    or access_context.agent_owner_email == owner_email
                )
            ):
                await assign_knowledgebase_to_agent(
                    session,
                    owner_email=owner_email,
                    knowledgebase_id=row.knowledgebase_id,
                    agent_id=access_context.agent_id,
                )
            await session.commit()
            return kb_model(row)

    async def update(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
        payload: KnowledgebaseUpdateRequest,
    ) -> KnowledgebaseModel | None:
        self.require_enabled()
        updates = payload.model_dump(exclude_unset=True)
        if "metadata_schema" in updates:
            updates["metadata_schema"] = _metadata_schema_with_defaults(
                updates.get("metadata_schema") or {}
            )
        if "settings" in updates:
            updates["settings"] = _validate_settings(updates.get("settings") or {})
        async with self._session_factory() as session:
            row = await update_knowledgebase(
                session,
                owner_email=owner_email,
                knowledgebase_id=knowledgebase_id,
                updates=updates,
            )
            await session.commit()
            return kb_model(row) if row is not None else None

    async def delete(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
    ) -> bool:
        self.require_enabled()
        async with self._session_factory() as session:
            deleted = await delete_knowledgebase(
                session,
                owner_email=owner_email,
                knowledgebase_id=knowledgebase_id,
            )
            await session.commit()
            return deleted

    async def list(
        self, *, owner_email: str, access_context: KnowledgebaseAccessContext | None = None
    ) -> list[KnowledgebaseModel]:
        self.require_enabled()
        context = access_context or KnowledgebaseAccessContext(actor_email=owner_email)
        async with self._session_factory() as session:
            rows = await list_available_knowledgebases(session, context=context)
            return [kb_model(row) for row in rows]

    async def get(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
        access_context: KnowledgebaseAccessContext | None = None,
    ) -> KnowledgebaseModel | None:
        self.require_enabled()
        context = access_context or KnowledgebaseAccessContext(actor_email=owner_email)
        async with self._session_factory() as session:
            resolved = await resolve_knowledgebase_access(
                session,
                knowledgebase_id=knowledgebase_id,
                context=context,
                mode="view",
            )
            return kb_model(resolved.knowledgebase) if resolved is not None else None

    async def attach(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
        artifact_id: str,
        metadata: dict[str, Any],
    ) -> KnowledgebaseArtifactModel | None:
        self.require_enabled()
        async with self._session_factory() as session:
            row = await attach_artifact_to_knowledgebase(
                session,
                owner_email=owner_email,
                knowledgebase_id=knowledgebase_id,
                artifact_id=artifact_id,
                metadata=metadata,
            )
            await session.commit()
            return kb_artifact_model(row) if row is not None else None

    async def detach(
        self, *, owner_email: str, knowledgebase_id: str, artifact_id: str
    ) -> KnowledgebaseArtifactModel | None:
        self.require_enabled()
        async with self._session_factory() as session:
            row = await detach_knowledgebase_artifact(
                session,
                owner_email=owner_email,
                knowledgebase_id=knowledgebase_id,
                artifact_id=artifact_id,
            )
            await session.commit()
            return kb_artifact_model(row) if row is not None else None

    async def artifacts(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
        access_context: KnowledgebaseAccessContext | None = None,
    ) -> list[KnowledgebaseArtifactModel] | None:
        self.require_enabled()
        context = access_context or KnowledgebaseAccessContext(actor_email=owner_email)
        async with self._session_factory() as session:
            resolved = await resolve_knowledgebase_access(
                session,
                knowledgebase_id=knowledgebase_id,
                context=context,
                mode="view",
            )
            if resolved is None:
                return None
            rows = await list_knowledgebase_artifacts(session, knowledgebase_id=knowledgebase_id)
            return [kb_artifact_model(row) for row in rows]

    async def jobs(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
        access_context: KnowledgebaseAccessContext | None = None,
    ) -> list[KnowledgebaseIndexJobModel] | None:
        self.require_enabled()
        context = access_context or KnowledgebaseAccessContext(actor_email=owner_email)
        async with self._session_factory() as session:
            resolved = await resolve_knowledgebase_access(
                session,
                knowledgebase_id=knowledgebase_id,
                context=context,
                mode="view",
            )
            if resolved is None:
                return None
            return [
                kb_job_model(row)
                for row in await list_knowledgebase_jobs(session, knowledgebase_id=knowledgebase_id)
            ]

    async def reindex_artifact(
        self, *, owner_email: str, knowledgebase_id: str, artifact_id: str
    ) -> KnowledgebaseIndexJobModel | None:
        self.require_enabled()
        async with self._session_factory() as session:
            row = await enqueue_knowledgebase_artifact_reindex(
                session,
                owner_email=owner_email,
                knowledgebase_id=knowledgebase_id,
                artifact_id=artifact_id,
            )
            await session.commit()
            return kb_job_model(row) if row is not None else None

    async def reindex(
        self, *, owner_email: str, knowledgebase_id: str
    ) -> list[KnowledgebaseIndexJobModel] | None:
        self.require_enabled()
        async with self._session_factory() as session:
            rows = await enqueue_knowledgebase_reindex(
                session, owner_email=owner_email, knowledgebase_id=knowledgebase_id
            )
            await session.commit()
            return [kb_job_model(row) for row in rows] if rows is not None else None

    async def retry_job(
        self, *, owner_email: str, knowledgebase_id: str, job_id: str
    ) -> KnowledgebaseIndexJobModel | None:
        self.require_enabled()
        async with self._session_factory() as session:
            row = await enqueue_retry_knowledgebase_job(
                session,
                owner_email=owner_email,
                knowledgebase_id=knowledgebase_id,
                job_id=job_id,
            )
            await session.commit()
            return kb_job_model(row) if row is not None else None

    async def diagnostics(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
        access_context: KnowledgebaseAccessContext | None = None,
    ) -> KnowledgebaseDiagnostics | None:
        self.require_enabled()
        context = access_context or KnowledgebaseAccessContext(actor_email=owner_email)
        async with self._session_factory() as session:
            resolved = await resolve_knowledgebase_access(
                session,
                knowledgebase_id=knowledgebase_id,
                context=context,
                mode="view",
            )
            if resolved is None:
                return None
            artifacts = await list_knowledgebase_artifacts(
                session, knowledgebase_id=knowledgebase_id
            )
            jobs = await list_knowledgebase_jobs(session, knowledgebase_id=knowledgebase_id)
            chunks = await list_knowledgebase_chunks(session, knowledgebase_id=knowledgebase_id)
        return KnowledgebaseDiagnostics(
            enabled=True,
            artifact_counts=dict(Counter(row.status for row in artifacts)),
            job_counts=dict(Counter(row.status for row in jobs)),
            chunk_count=len(chunks),
            backend_health=await self._vector_backend.health(),
        )

    async def search(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
        payload: KnowledgebaseSearchRequest,
        access_context: KnowledgebaseAccessContext | None = None,
    ) -> KnowledgebaseSearchResponse | None:
        self.require_enabled()
        context = access_context or KnowledgebaseAccessContext(actor_email=owner_email)
        async with self._session_factory() as session:
            resolved = await resolve_knowledgebase_access(
                session,
                knowledgebase_id=knowledgebase_id,
                context=context,
                mode="use",
            )
            if resolved is None:
                return None
            kb = resolved.knowledgebase
            try:
                _validate_filters(payload.filters, kb.metadata_schema or {})
            except ValueError as exc:
                raise KnowledgebaseValidationError(str(exc)) from exc
            has_residual_filters = _has_residual_filters(payload.filters)
        query_vector = (
            await self._llm.embed(
                [payload.query], task_type="embedding", acting_user_email=context.actor_email
            )
        )[0]
        overfetch_limit = max(payload.limit * (8 if has_residual_filters else 4), payload.limit)
        hits = await self._vector_backend.search(
            query_vector,
            limit=overfetch_limit,
            filters=_vector_filters(
                owner_email=kb.owner_email,
                knowledgebase_id=knowledgebase_id,
                filters=payload.filters,
            ),
            sparse_vector=sparse_vector_from_text(payload.query),
        )
        fused: defaultdict[str, float] = defaultdict(float)
        for hit in hits:
            fused[_dense_hit_chunk_id(hit)] = hit.score
        async with self._session_factory() as session:
            rows = {
                chunk.chunk_id: chunk
                for chunk in await list_knowledgebase_chunks(
                    session, knowledgebase_id=knowledgebase_id
                )
                if chunk.chunk_id in fused
            }
        if payload.filters:
            rows = {
                chunk.chunk_id: chunk
                for chunk in _apply_filters(list(rows.values()), payload.filters)
            }
        matches: list[KnowledgebaseSearchMatch] = []
        for chunk_id in sorted(fused, key=lambda value: fused[value], reverse=True)[
            : payload.limit
        ]:
            chunk = rows.get(chunk_id)
            if chunk is None:
                continue
            locator = dict(chunk.locator)
            matches.append(
                KnowledgebaseSearchMatch(
                    chunk_id=chunk.chunk_id,
                    artifact_id=chunk.artifact_id,
                    snippet=chunk.text[:800],
                    score=fused[chunk_id],
                    score_breakdown={
                        "fusion": fused[chunk_id],
                    },
                    metadata=chunk.metadata_json or {},
                    citation=KnowledgebaseSourceCitation(
                        artifact_id=chunk.artifact_id,
                        filename=(chunk.metadata_json or {}).get("filename"),
                        mime_type=(chunk.metadata_json or {}).get("mime_type"),
                        locator=locator,
                    ),
                )
            )
        return KnowledgebaseSearchResponse(
            matches=matches,
            diagnostics={
                "retrieval_mode": "qdrant_native_hybrid",
                "sparse_algorithm": SPARSE_ALGORITHM,
                "qdrant_fusion": "rrf",
                "candidates_returned": len(hits),
                "post_filter_candidates": len(rows),
                "matches": len(matches),
            },
        )

    async def source_context(
        self,
        *,
        owner_email: str,
        knowledgebase_id: str,
        chunk_id: str,
        before_chars: int = 500,
        after_chars: int = 500,
        access_context: KnowledgebaseAccessContext | None = None,
    ) -> KnowledgebaseSourceContextResponse | None:
        self.require_enabled()
        context = access_context or KnowledgebaseAccessContext(actor_email=owner_email)
        async with self._session_factory() as session:
            resolved = await resolve_knowledgebase_access(
                session,
                knowledgebase_id=knowledgebase_id,
                context=context,
                mode="use",
            )
            if resolved is None:
                return None
            chunk = await get_knowledgebase_chunk(
                session, knowledgebase_id=knowledgebase_id, chunk_id=chunk_id
            )
            if chunk is None:
                return None
            artifact = await get_artifact_record(session, chunk.artifact_id)
            warnings = []
            if artifact is None or artifact.status == "deleted":
                warnings.append("canonical_artifact_missing")
                return KnowledgebaseSourceContextResponse(
                    chunk_id=chunk.chunk_id,
                    artifact_id=chunk.artifact_id,
                    text=chunk.text,
                    locator=chunk.locator,
                    warnings=warnings,
                )
            elif (
                chunk.artifact_hash
                and artifact.content_hash
                and chunk.artifact_hash != artifact.content_hash
            ):
                warnings.append("source_hash_mismatch")
            context_text = chunk.text
            if self._artifact_store is None:
                warnings.append("artifact_store_unavailable")
            else:
                try:
                    content, _ = await self._artifact_store.async_load(
                        artifact.namespace, artifact.object_id, artifact.filename
                    )
                    document = extract_artifact_bytes(
                        content, filename=artifact.filename, mime_type=artifact.mime_type
                    )
                    full_text = "\n".join(span.text for span in document.spans)
                    char_start = chunk.locator.get("char_start")
                    char_end = chunk.locator.get("char_end")
                    if isinstance(char_start, int) and isinstance(char_end, int):
                        start = max(0, char_start - before_chars)
                        end = min(len(full_text), char_end + after_chars)
                        context_text = full_text[start:end]
                    else:
                        needle_index = full_text.find(chunk.text)
                        if needle_index >= 0:
                            start = max(0, needle_index - before_chars)
                            end = min(len(full_text), needle_index + len(chunk.text) + after_chars)
                            context_text = full_text[start:end]
                        else:
                            warnings.append("source_locator_unresolved")
                except Exception as exc:
                    warnings.append(f"source_context_extract_failed:{type(exc).__name__}")
            return KnowledgebaseSourceContextResponse(
                chunk_id=chunk.chunk_id,
                artifact_id=chunk.artifact_id,
                text=context_text,
                locator=chunk.locator,
                warnings=warnings,
            )
