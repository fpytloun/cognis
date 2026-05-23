"""Knowledgebase API routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_current_user
from cognis.knowledgebase.access import KnowledgebaseAccessContext
from cognis.knowledgebase.service import KnowledgebaseRequestError, KnowledgebaseValidationError
from cognis.models.knowledgebase import (
    KnowledgebaseArtifactModel,
    KnowledgebaseAttachRequest,
    KnowledgebaseBulkAttachRequest,
    KnowledgebaseCreateRequest,
    KnowledgebaseDiagnostics,
    KnowledgebaseHealth,
    KnowledgebaseIndexJobModel,
    KnowledgebaseModel,
    KnowledgebaseSearchRequest,
    KnowledgebaseSearchResponse,
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
    raise api_exception(
        404, "not_found", "Knowledgebase feature is not available", details={"reason": str(exc)}
    )


def _raise_disabled(exc: RuntimeError) -> None:
    _disabled_error(exc)
    raise AssertionError("unreachable")


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


@router.get("/health", response_model=KnowledgebaseHealth)
async def knowledgebase_health(request: Request) -> KnowledgebaseHealth:
    require_current_user(request)
    return await _service(request).health()


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
            KnowledgebaseAttachRequest(artifact_id=item.artifact_id, metadata=item.metadata)
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
    request: Request, knowledgebase_id: str
) -> list[KnowledgebaseIndexJobModel]:
    user = require_current_user(request)
    try:
        rows = await _service(request).jobs(
            owner_email=user.email,
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(request),
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
