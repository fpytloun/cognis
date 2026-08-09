"""Knowledgebase API routes."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from starlette.responses import StreamingResponse

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_current_user
from cognis.knowledgebase.access import KnowledgebaseAccessContext
from cognis.knowledgebase.service import (
    KnowledgebaseFacetLimitError,
    KnowledgebaseNotReadyError,
    KnowledgebaseRequestError,
    KnowledgebaseValidationError,
    normalize_source_path,
)
from cognis.models.knowledgebase import (
    KnowledgebaseArtifactModel,
    KnowledgebaseAskRequest,
    KnowledgebaseAskResponse,
    KnowledgebaseAttachRequest,
    KnowledgebaseBulkAttachRequest,
    KnowledgebaseCapabilities,
    KnowledgebaseCreateRequest,
    KnowledgebaseDiagnostics,
    KnowledgebaseDocumentContent,
    KnowledgebaseDocumentDetail,
    KnowledgebaseDocumentListResponse,
    KnowledgebaseDocumentUpdateRequest,
    KnowledgebaseFacetRequest,
    KnowledgebaseFacetResponse,
    KnowledgebaseHealth,
    KnowledgebaseIndexJobModel,
    KnowledgebaseIngestOutcome,
    KnowledgebaseIngestResponse,
    KnowledgebaseModel,
    KnowledgebaseSearchRequest,
    KnowledgebaseSearchResponse,
    KnowledgebaseShareCandidate,
    KnowledgebaseShareModel,
    KnowledgebaseShareRequest,
    KnowledgebaseSourceContextRequest,
    KnowledgebaseSourceContextResponse,
    KnowledgebaseUpdateRequest,
)
from cognis.store.queries import (
    assign_knowledgebase_to_agent,
    list_knowledgebase_agent_assignments,
    unassign_knowledgebase_from_agent,
)

router = APIRouter(prefix="/api/v1/knowledgebases", tags=["knowledgebases"])


def _service(request: Request):
    service = getattr(request.app.state, "knowledgebase_service", None)
    if service is None:
        raise api_exception(404, "not_found", "Knowledgebase feature is not available")
    return service


def _disabled_error(exc: RuntimeError) -> None:
    if isinstance(exc, KnowledgebaseNotReadyError):
        raise api_exception(
            503,
            "knowledgebase_not_ready",
            str(exc),
        )
    raise api_exception(
        404, "not_found", "Knowledgebase feature is not available", details={"reason": str(exc)}
    )


def _raise_disabled(exc: RuntimeError) -> None:
    _disabled_error(exc)
    raise AssertionError("unreachable")


def _raise_product_error(exc: KnowledgebaseRequestError) -> None:
    message = str(exc)
    if "size limit" in message:
        raise api_exception(413, "content_too_large", message) from exc
    if "not directly readable" in message or "unsupported document type" in message:
        raise api_exception(415, "unsupported_media_type", message) from exc
    raise api_exception(400, "validation_error", message) from exc


def _session_factory(request: Request):
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise api_exception(500, "internal_error", "Session factory unavailable")
    return session_factory


def _access_context(request: Request) -> KnowledgebaseAccessContext:
    user = require_current_user(request)
    runtime_access = getattr(request.state, "runtime_access", None)
    if not isinstance(runtime_access, dict):
        runtime_access = {}
    agent_id = runtime_access.get("agent_id")
    agent_owner_email = runtime_access.get("agent_owner_email")
    return KnowledgebaseAccessContext(
        actor_email=user.email,
        agent_id=agent_id if isinstance(agent_id, str) else None,
        agent_owner_email=agent_owner_email if isinstance(agent_owner_email, str) else None,
    )


async def _close_uploads(files: list[UploadFile]) -> None:
    for upload in files:
        await upload.close()


@router.get("/health", response_model=KnowledgebaseHealth)
async def knowledgebase_health(request: Request) -> KnowledgebaseHealth:
    require_current_user(request)
    return await _service(request).health()


@router.get("/capabilities", response_model=KnowledgebaseCapabilities)
async def knowledgebase_capabilities(request: Request) -> KnowledgebaseCapabilities:
    require_current_user(request)
    service = getattr(request.app.state, "knowledgebase_service", None)
    if service is None:
        return KnowledgebaseCapabilities(
            enabled=False,
            vector_backend="unavailable",
            backend_ready=False,
            embedding_ready=False,
            indexer_ready=False,
            ask_ready=False,
            notes=["Knowledgebase service is not configured."],
        )
    try:
        return await service.capabilities()
    except Exception:
        return KnowledgebaseCapabilities(
            enabled=bool(getattr(service, "enabled", False)),
            vector_backend="unavailable",
            backend_ready=False,
            embedding_ready=False,
            indexer_ready=False,
            ask_ready=False,
            notes=["Knowledgebase capability status is temporarily unavailable."],
        )


@router.post("/", response_model=KnowledgebaseModel)
async def knowledgebase_create(
    request: Request, payload: KnowledgebaseCreateRequest
) -> KnowledgebaseModel:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        return await _service(request).create(
            owner_email=user.email,
            name=payload.name,
            description=payload.description,
            metadata_schema=payload.metadata_schema,
            settings=payload.settings,
            access_context=_access_context(request),
        )
    except KnowledgebaseRequestError as exc:
        raise api_exception(400, "invalid_knowledgebase_settings", str(exc)) from exc
    except RuntimeError as exc:
        _raise_disabled(exc)


@router.get("/", response_model=list[KnowledgebaseModel])
async def knowledgebase_list(request: Request) -> list[KnowledgebaseModel]:
    user = require_current_user(request)
    try:
        return await _service(request).list(
            owner_email=user.email, access_context=_access_context(request)
        )
    except RuntimeError as exc:
        _raise_disabled(exc)


@router.get("/{knowledgebase_id}", response_model=KnowledgebaseModel)
async def knowledgebase_get(request: Request, knowledgebase_id: str) -> KnowledgebaseModel:
    user = require_current_user(request)
    try:
        kb = await _service(request).get(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(request),
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if kb is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return kb


@router.get("/{knowledgebase_id}/shares", response_model=list[KnowledgebaseShareModel])
async def knowledgebase_shares(
    request: Request, knowledgebase_id: str
) -> list[KnowledgebaseShareModel]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        rows = await _service(request).list_shares(
            owner_email=user.email, knowledgebase_id=knowledgebase_id
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if rows is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return rows


@router.get(
    "/{knowledgebase_id}/shares/candidates",
    response_model=list[KnowledgebaseShareCandidate],
)
async def knowledgebase_share_candidates(
    request: Request, knowledgebase_id: str, q: str | None = None
) -> list[KnowledgebaseShareCandidate]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        rows = await _service(request).share_candidates(
            owner_email=user.email, knowledgebase_id=knowledgebase_id, query=q
        )
    except KnowledgebaseRequestError as exc:
        raise api_exception(400, "invalid_share_candidate_query", str(exc)) from exc
    except RuntimeError as exc:
        _raise_disabled(exc)
    if rows is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return rows


@router.put("/{knowledgebase_id}/shares", response_model=KnowledgebaseShareModel)
async def knowledgebase_share_grant(
    request: Request, knowledgebase_id: str, payload: KnowledgebaseShareRequest
) -> KnowledgebaseShareModel:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        row = await _service(request).grant_share(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            payload=payload,
        )
    except KnowledgebaseRequestError as exc:
        raise api_exception(400, "invalid_share", str(exc)) from exc
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return row


@router.delete("/{knowledgebase_id}/shares/{user_email}", response_model=dict[str, bool])
async def knowledgebase_share_revoke(
    request: Request, knowledgebase_id: str, user_email: str
) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        revoked = await _service(request).revoke_share(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            user_email=user_email,
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if revoked is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return {"revoked": revoked}


@router.patch("/{knowledgebase_id}", response_model=KnowledgebaseModel)
async def knowledgebase_update(
    request: Request, knowledgebase_id: str, payload: KnowledgebaseUpdateRequest
) -> KnowledgebaseModel:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        kb = await _service(request).update(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            payload=payload,
        )
    except KnowledgebaseRequestError as exc:
        raise api_exception(400, "invalid_knowledgebase_settings", str(exc)) from exc
    except RuntimeError as exc:
        _raise_disabled(exc)
        raise AssertionError("unreachable") from exc
    if kb is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return kb


@router.post("/{knowledgebase_id}/artifacts", response_model=KnowledgebaseArtifactModel)
async def knowledgebase_attach_artifact(
    request: Request, knowledgebase_id: str, payload: KnowledgebaseAttachRequest
) -> KnowledgebaseArtifactModel:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        row = await _service(request).attach(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            artifact_id=payload.artifact_id,
            source_path=payload.source_path,
            metadata=payload.metadata,
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Knowledgebase or artifact not found")
    return row


@router.post("/{knowledgebase_id}/artifacts/bulk", response_model=list[KnowledgebaseArtifactModel])
async def knowledgebase_attach_artifacts_bulk(
    request: Request, knowledgebase_id: str, payload: KnowledgebaseBulkAttachRequest
) -> list[KnowledgebaseArtifactModel]:
    results = []
    if payload.artifact_ids:
        items = [
            KnowledgebaseAttachRequest(artifact_id=artifact_id, metadata=payload.metadata)
            for artifact_id in payload.artifact_ids
        ]
    else:
        items = [
            KnowledgebaseAttachRequest(
                artifact_id=item.artifact_id,
                source_path=item.source_path,
                metadata=item.metadata,
            )
            for item in payload.items
        ]
    for item in items:
        row = await knowledgebase_attach_artifact(
            request,
            knowledgebase_id,
            item,
        )
        results.append(row)
    return results


@router.delete("/{knowledgebase_id}", response_model=dict[str, bool])
async def knowledgebase_delete(request: Request, knowledgebase_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        deleted = await _service(request).delete(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
        raise AssertionError("unreachable") from exc
    if not deleted:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return {"deleted": True}


@router.get("/{knowledgebase_id}/artifacts", response_model=list[KnowledgebaseArtifactModel])
async def knowledgebase_artifacts(
    request: Request, knowledgebase_id: str
) -> list[KnowledgebaseArtifactModel]:
    user = require_current_user(request)
    try:
        rows = await _service(request).artifacts(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(request),
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if rows is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return rows


@router.get(
    "/{knowledgebase_id}/documents",
    response_model=KnowledgebaseDocumentListResponse,
)
async def knowledgebase_documents(
    request: Request,
    knowledgebase_id: str,
    status: str | None = None,
    path_prefix: str | None = None,
    q: str | None = None,
    sort: Literal["path", "updated_at"] = "path",
    direction: Literal["asc", "desc"] = "asc",
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> KnowledgebaseDocumentListResponse:
    user = require_current_user(request)
    try:
        rows = await _service(request).documents(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(request),
            status=status,
            path_prefix=path_prefix,
            query=q,
            sort=sort,
            direction=direction,
            cursor=cursor,
            limit=limit,
        )
    except KnowledgebaseRequestError as exc:
        _raise_product_error(exc)
    except RuntimeError as exc:
        _raise_disabled(exc)
    if rows is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return rows


@router.post(
    "/{knowledgebase_id}/documents",
    response_model=KnowledgebaseIngestResponse,
)
async def knowledgebase_ingest_documents(
    request: Request,
    knowledgebase_id: str,
    files: Annotated[list[UploadFile], File(alias="files[]")],
    paths: Annotated[list[str] | None, Form(alias="paths[]")] = None,
    metadata: Annotated[str | None, Form()] = None,
    conflict_policy: Annotated[Literal["skip", "replace", "keep_both"], Form()] = "replace",
) -> KnowledgebaseIngestResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    context = _access_context(request)
    if context.agent_id is not None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    if not files or len(files) > 25:
        await _close_uploads(files)
        raise api_exception(
            400,
            "validation_error",
            "documents batch must contain between 1 and 25 files",
        )
    if paths is not None and len(paths) != len(files):
        await _close_uploads(files)
        raise api_exception(
            400,
            "validation_error",
            "paths[] must contain exactly one path for each file",
        )
    if (
        metadata is not None
        and len(metadata.encode("utf-8")) > _service(request).max_metadata_json_bytes
    ):
        await _close_uploads(files)
        raise api_exception(400, "validation_error", "metadata exceeds size limit")
    if (
        paths is not None
        and sum(len(path.encode("utf-8")) for path in paths)
        > _service(request).max_total_path_bytes
    ):
        await _close_uploads(files)
        raise api_exception(400, "validation_error", "paths[] exceed total size limit")
    if paths is not None and any(len(path.encode("utf-8")) > 1024 for path in paths):
        await _close_uploads(files)
        raise api_exception(400, "validation_error", "source path exceeds size limit")
    try:
        metadata_value = json.loads(metadata) if metadata is not None else None
    except json.JSONDecodeError as exc:
        await _close_uploads(files)
        raise api_exception(400, "validation_error", "metadata must be valid JSON") from exc
    if metadata_value is not None and not isinstance(metadata_value, dict):
        await _close_uploads(files)
        raise api_exception(400, "validation_error", "metadata must be a JSON object")
    service = _service(request)
    try:
        await service.require_index_ready()
    except RuntimeError as exc:
        await _close_uploads(files)
        _raise_disabled(exc)
    total_bytes = 0
    try:
        for upload in files:
            file_bytes = 0
            while chunk := await upload.read(1024 * 1024):
                file_bytes += len(chunk)
                total_bytes += len(chunk)
                if file_bytes > service.max_artifact_size_bytes:
                    raise api_exception(
                        413, "content_too_large", "document exceeds upload size limit"
                    )
                if total_bytes > service.max_total_upload_bytes:
                    raise api_exception(
                        413,
                        "content_too_large",
                        "document batch exceeds aggregate upload size limit",
                    )
            await upload.seek(0)
    except BaseException:
        await _close_uploads(files)
        raise
    outcomes: list[KnowledgebaseIngestOutcome] = []
    batch_paths: set[str] = set()
    for index, upload in enumerate(files):
        filename = upload.filename or "document"
        source_path = paths[index] if paths is not None else filename
        try:
            normalized = normalize_source_path(source_path)
            if normalized in batch_paths:
                raise KnowledgebaseRequestError("duplicate source path in batch")
            batch_paths.add(normalized)
            content = bytearray()
            while chunk := await upload.read(1024 * 1024):
                content.extend(chunk)
                if len(content) > service.max_artifact_size_bytes:
                    raise KnowledgebaseRequestError("document exceeds upload size limit")
            item_outcomes = await service.ingest_documents(
                owner_email=user.email,
                knowledgebase_id=knowledgebase_id,
                files=[
                    (
                        filename,
                        content,
                        upload.content_type or "application/octet-stream",
                        normalized,
                    )
                ],
                metadata=metadata_value,
                conflict_policy=conflict_policy,
            )
            if item_outcomes is None:
                raise api_exception(404, "not_found", "Knowledgebase not found")
            outcomes.extend(item_outcomes)
        except KnowledgebaseRequestError as exc:
            outcomes.append(
                KnowledgebaseIngestOutcome(
                    filename=filename,
                    source_path=source_path,
                    status="failed",
                    error_code="validation_error",
                    message=str(exc)[:300],
                )
            )
        except RuntimeError as exc:
            _raise_disabled(exc)
        finally:
            await upload.close()
    return KnowledgebaseIngestResponse(outcomes=outcomes)


@router.get(
    "/{knowledgebase_id}/documents/{kb_artifact_id}",
    response_model=KnowledgebaseDocumentDetail,
)
async def knowledgebase_document(
    request: Request, knowledgebase_id: str, kb_artifact_id: str
) -> KnowledgebaseDocumentDetail:
    user = require_current_user(request)
    try:
        row = await _service(request).document(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            kb_artifact_id=kb_artifact_id,
            access_context=_access_context(request),
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Knowledgebase document not found")
    return row


@router.get(
    "/{knowledgebase_id}/documents/{kb_artifact_id}/content",
    response_model=KnowledgebaseDocumentContent,
)
async def knowledgebase_document_content(
    request: Request,
    knowledgebase_id: str,
    kb_artifact_id: str,
    content_mode: Literal["source", "extracted"] = Query(default="extracted"),
) -> KnowledgebaseDocumentContent:
    user = require_current_user(request)
    try:
        row = await _service(request).document_content(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            kb_artifact_id=kb_artifact_id,
            content_mode=content_mode,
            access_context=_access_context(request),
        )
    except KnowledgebaseRequestError as exc:
        _raise_product_error(exc)
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Knowledgebase document not found")
    return row


@router.get("/{knowledgebase_id}/documents/{source_kb_artifact_id}/resources/{resource_path:path}")
async def knowledgebase_document_resource(
    request: Request,
    knowledgebase_id: str,
    source_kb_artifact_id: str,
    resource_path: str,
) -> Response:
    user = require_current_user(request)
    try:
        resource = await _service(request).document_resource(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            source_kb_artifact_id=source_kb_artifact_id,
            resource_path=resource_path,
            access_context=_access_context(request),
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if resource is None:
        raise api_exception(404, "not_found", "Knowledgebase resource not found")
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", resource.filename) or "resource"
    disposition = "inline" if resource.inline else "attachment"
    headers = {
        "Content-Disposition": (
            f'{disposition}; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(resource.filename, safe='')}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, no-store",
    }
    return StreamingResponse(
        iter([resource.content]),
        media_type=resource.mime_type,
        headers=headers,
    )


@router.patch(
    "/{knowledgebase_id}/documents/{kb_artifact_id}",
    response_model=KnowledgebaseDocumentDetail,
)
async def knowledgebase_update_document(
    request: Request,
    knowledgebase_id: str,
    kb_artifact_id: str,
    payload: KnowledgebaseDocumentUpdateRequest,
) -> KnowledgebaseDocumentDetail:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    if _access_context(request).agent_id is not None:
        raise api_exception(404, "not_found", "Knowledgebase document not found")
    try:
        row = await _service(request).update_document(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            kb_artifact_id=kb_artifact_id,
            payload=payload,
        )
    except KnowledgebaseRequestError as exc:
        _raise_product_error(exc)
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Knowledgebase document not found")
    return row


@router.delete(
    "/{knowledgebase_id}/artifacts/{artifact_id}", response_model=KnowledgebaseArtifactModel
)
async def knowledgebase_detach_artifact(
    request: Request, knowledgebase_id: str, artifact_id: str
) -> KnowledgebaseArtifactModel:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        row = await _service(request).detach(
            owner_email=user.email, knowledgebase_id=knowledgebase_id, artifact_id=artifact_id
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Knowledgebase artifact not found")
    return row


@router.get("/{knowledgebase_id}/jobs", response_model=list[KnowledgebaseIndexJobModel])
async def knowledgebase_jobs(
    request: Request,
    knowledgebase_id: str,
    status: str | None = None,
    job_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=100),
) -> list[KnowledgebaseIndexJobModel]:
    user = require_current_user(request)
    try:
        rows = await _service(request).jobs(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(request),
            status=status,
            job_type=job_type,
            limit=limit,
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if rows is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return rows


@router.post(
    "/{knowledgebase_id}/artifacts/{artifact_id}/reindex",
    response_model=KnowledgebaseIndexJobModel,
)
async def knowledgebase_reindex_artifact(
    request: Request, knowledgebase_id: str, artifact_id: str
) -> KnowledgebaseIndexJobModel:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        row = await _service(request).reindex_artifact(
            owner_email=user.email, knowledgebase_id=knowledgebase_id, artifact_id=artifact_id
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Knowledgebase artifact not found")
    return row


@router.post("/{knowledgebase_id}/reindex", response_model=list[KnowledgebaseIndexJobModel])
async def knowledgebase_reindex(
    request: Request, knowledgebase_id: str
) -> list[KnowledgebaseIndexJobModel]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        rows = await _service(request).reindex(
            owner_email=user.email, knowledgebase_id=knowledgebase_id
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if rows is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return rows


@router.get("/{knowledgebase_id}/status", response_model=KnowledgebaseDiagnostics)
async def knowledgebase_status(request: Request, knowledgebase_id: str) -> KnowledgebaseDiagnostics:
    return await knowledgebase_diagnostics(request, knowledgebase_id)


@router.post("/{knowledgebase_id}/jobs/{job_id}/retry", response_model=KnowledgebaseIndexJobModel)
async def knowledgebase_retry_job(
    request: Request, knowledgebase_id: str, job_id: str
) -> KnowledgebaseIndexJobModel:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        row = await _service(request).retry_job(
            owner_email=user.email, knowledgebase_id=knowledgebase_id, job_id=job_id
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Retryable knowledgebase job not found")
    return row


@router.post("/{knowledgebase_id}/jobs/{job_id}/cancel", response_model=KnowledgebaseIndexJobModel)
async def knowledgebase_cancel_job(
    request: Request, knowledgebase_id: str, job_id: str
) -> KnowledgebaseIndexJobModel:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        row = await _service(request).cancel_job(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            job_id=job_id,
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if row is None:
        raise api_exception(404, "not_found", "Queued knowledgebase job not found")
    return row


@router.get("/{knowledgebase_id}/diagnostics", response_model=KnowledgebaseDiagnostics)
async def knowledgebase_diagnostics(
    request: Request, knowledgebase_id: str
) -> KnowledgebaseDiagnostics:
    user = require_current_user(request)
    try:
        diagnostics = await _service(request).diagnostics(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(request),
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if diagnostics is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return diagnostics


@router.get("/{knowledgebase_id}/agents", response_model=list[str])
async def knowledgebase_agent_assignments(request: Request, knowledgebase_id: str) -> list[str]:
    user = require_current_user(request)
    try:
        _service(request).require_enabled()
        async with _session_factory(request)() as session:
            rows = await list_knowledgebase_agent_assignments(
                session, owner_email=user.email, knowledgebase_id=knowledgebase_id
            )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if rows is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return rows


@router.post("/{knowledgebase_id}/agents/{agent_id}", response_model=dict[str, bool])
async def knowledgebase_assign_agent(
    request: Request, knowledgebase_id: str, agent_id: str
) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        _service(request).require_enabled()
        async with _session_factory(request)() as session:
            ok = await assign_knowledgebase_to_agent(
                session,
                owner_email=user.email,
                knowledgebase_id=knowledgebase_id,
                agent_id=agent_id,
            )
            await session.commit()
    except RuntimeError as exc:
        _raise_disabled(exc)
    if not ok:
        raise api_exception(404, "not_found", "Knowledgebase or agent not found")
    return {"assigned": True}


@router.delete("/{knowledgebase_id}/agents/{agent_id}", response_model=dict[str, bool])
async def knowledgebase_unassign_agent(
    request: Request, knowledgebase_id: str, agent_id: str
) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        _service(request).require_enabled()
        async with _session_factory(request)() as session:
            ok = await unassign_knowledgebase_from_agent(
                session,
                owner_email=user.email,
                knowledgebase_id=knowledgebase_id,
                agent_id=agent_id,
            )
            await session.commit()
    except RuntimeError as exc:
        _raise_disabled(exc)
    if not ok:
        raise api_exception(404, "not_found", "Knowledgebase or agent not found")
    return {"assigned": False}


@router.post("/{knowledgebase_id}/search", response_model=KnowledgebaseSearchResponse)
async def knowledgebase_search(
    request: Request, knowledgebase_id: str, payload: KnowledgebaseSearchRequest
) -> KnowledgebaseSearchResponse:
    user = require_current_user(request)
    try:
        response = await _service(request).search(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            payload=payload,
            access_context=_access_context(request),
        )
    except KnowledgebaseValidationError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except RuntimeError as exc:
        _raise_disabled(exc)
    if response is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return response


@router.post("/{knowledgebase_id}/facets", response_model=KnowledgebaseFacetResponse)
async def knowledgebase_facets(
    request: Request, knowledgebase_id: str, payload: KnowledgebaseFacetRequest
) -> KnowledgebaseFacetResponse:
    user = require_current_user(request)
    try:
        response = await _service(request).facets(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            payload=payload,
            access_context=_access_context(request),
        )
    except KnowledgebaseFacetLimitError as exc:
        raise api_exception(422, "facet_document_limit", str(exc)) from exc
    except KnowledgebaseValidationError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except RuntimeError as exc:
        _raise_disabled(exc)
    if response is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return response


@router.post("/{knowledgebase_id}/ask", response_model=KnowledgebaseAskResponse)
async def knowledgebase_ask(
    request: Request, knowledgebase_id: str, payload: KnowledgebaseAskRequest
) -> KnowledgebaseAskResponse:
    user = require_current_user(request)
    try:
        response = await _service(request).ask(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            payload=payload,
            access_context=_access_context(request),
        )
    except KnowledgebaseValidationError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except RuntimeError as exc:
        _raise_disabled(exc)
    if response is None:
        raise api_exception(404, "not_found", "Knowledgebase not found")
    return response


@router.post(
    "/{knowledgebase_id}/source-context", response_model=KnowledgebaseSourceContextResponse
)
async def knowledgebase_source_context(
    request: Request, knowledgebase_id: str, payload: KnowledgebaseSourceContextRequest
) -> KnowledgebaseSourceContextResponse:
    user = require_current_user(request)
    try:
        response = await _service(request).source_context(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            chunk_id=payload.chunk_id,
            before_chars=payload.before_chars,
            after_chars=payload.after_chars,
            access_context=_access_context(request),
        )
    except RuntimeError as exc:
        _raise_disabled(exc)
    if response is None:
        raise api_exception(404, "not_found", "Knowledgebase chunk not found")
    return response
