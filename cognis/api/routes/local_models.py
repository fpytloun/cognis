"""Declarative local-model desired-state and managed-runtime routes.

Deployment routes persist controller-owned intent. Exact-target runtime routes
create durable operations and dispatch managed Ollama work through authorized
executor connections.
"""

from __future__ import annotations

from typing import Any, Literal, NoReturn, cast

from fastapi import APIRouter, Query, Request, Response

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.api.models import LLMProviderResponse
from cognis.api.serializers import llm_provider_to_response
from cognis.core.local_model_catalog import CatalogUpstreamError, LocalModelCatalog
from cognis.core.local_model_fit import FitExecutor, plan_local_model_fit
from cognis.core.local_model_providers import LocalModelProviderService
from cognis.core.local_model_runtime import LocalModelRuntimeUnavailable
from cognis.core.local_model_service import (
    LocalModelAccessError,
    LocalModelDependencyError,
    LocalModelDeploymentService,
    LocalModelNotFoundError,
    LocalModelValidationError,
)
from cognis.models.local_models import (
    LocalModelCatalogItem,
    LocalModelCatalogResponse,
    LocalModelCatalogSource,
    LocalModelDeploymentCreateRequest,
    LocalModelDeploymentResponse,
    LocalModelDeploymentStatusResponse,
    LocalModelDeploymentUpdateRequest,
    LocalModelFitPlanRequest,
    LocalModelFitPlanResponse,
    LocalModelManagedDeploymentCreateRequest,
    LocalModelManagedDeploymentCreateResponse,
    LocalModelManagedProviderAttachRequest,
    LocalModelManagedProviderAttachResponse,
    LocalModelOperationResponse,
    LocalModelProviderFindOrCreateRequest,
    LocalModelProviderFindOrCreateResponse,
    LocalModelProviderRecommendationRequest,
    LocalModelProviderRecommendationResponse,
    LocalModelRuntimeOperationCreateRequest,
    LocalModelSelector,
    LocalModelTargetStatusResponse,
    OllamaRuntimeStatus,
    ProviderLocalModelUpsertRequest,
)
from cognis.ownership import is_shared_owner_email

router = APIRouter(tags=["local-models"])


def _validation_details(message: str) -> dict[str, object] | None:
    if any(
        marker in message
        for marker in (
            "eligible managed hosts",
            "managed Ollama",
            "Local inference",
            "Ollama models",
        )
    ):
        return {
            "action": "configure_executor_local_inference",
            "action_url": "/settings?tab=executors",
        }
    return None


def _translate_service_error(exc: Exception) -> NoReturn:
    if isinstance(exc, LocalModelNotFoundError):
        raise api_exception(404, "not_found", str(exc)) from exc
    if isinstance(exc, LocalModelAccessError):
        raise api_exception(403, "forbidden", str(exc)) from exc
    if isinstance(exc, LocalModelDependencyError):
        raise api_exception(
            409,
            "local_model_dependencies",
            str(exc),
            details={"deployment_ids": exc.deployment_ids},
        ) from exc
    if isinstance(exc, LocalModelValidationError):
        message = str(exc)
        raise api_exception(
            422,
            "validation_error",
            message,
            details=_validation_details(message),
        ) from exc
    raise exc


def _deployment_response(row: object) -> LocalModelDeploymentResponse:
    return LocalModelDeploymentResponse(
        deployment_id=str(row.deployment_id),  # type: ignore[attr-defined]
        owner_email=str(row.owner_email),  # type: ignore[attr-defined]
        shared=is_shared_owner_email(row.owner_email),  # type: ignore[attr-defined]
        runtime_type=row.runtime_type,  # type: ignore[attr-defined]
        requested_ref=row.requested_ref,  # type: ignore[attr-defined]
        canonical_name=row.canonical_name,  # type: ignore[attr-defined]
        runtime_name=row.runtime_name,  # type: ignore[attr-defined]
        source=row.source,  # type: ignore[attr-defined]
        digest=row.digest,  # type: ignore[attr-defined]
        revision=row.revision,  # type: ignore[attr-defined]
        selector=LocalModelSelector.model_validate(row.selector),  # type: ignore[attr-defined]
        desired_state=row.desired_state,  # type: ignore[attr-defined]
        update_policy=row.update_policy,  # type: ignore[attr-defined]
        prune_policy=row.prune_policy,  # type: ignore[attr-defined]
        max_parallel=row.max_parallel,  # type: ignore[attr-defined]
        generation=row.generation,  # type: ignore[attr-defined]
        provider_id=row.provider_id,  # type: ignore[attr-defined]
        lifecycle_state="managed" if row.provider_id is not None else "needs_provider",  # type: ignore[attr-defined]
        capacity_override_acknowledged=row.capacity_override_acknowledged,  # type: ignore[attr-defined]
        capacity_assessment_generation=row.capacity_assessment_generation,  # type: ignore[attr-defined]
        reconcile_requested_at=row.reconcile_requested_at,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


def _target_response(row: object) -> LocalModelTargetStatusResponse:
    return LocalModelTargetStatusResponse.model_validate(
        {column: getattr(row, column) for column in LocalModelTargetStatusResponse.model_fields}
    )


def _operation_response(row: object) -> LocalModelOperationResponse:
    return LocalModelOperationResponse.model_validate(
        {column: getattr(row, column) for column in LocalModelOperationResponse.model_fields}
    )


def _trigger_reconcile(
    request: Request,
    *,
    deployment_id: str | None = None,
    executor_id: str | None = None,
) -> None:
    reconciler = getattr(request.app.state, "local_model_reconciler", None)
    if reconciler is not None:
        reconciler.trigger(
            deployment_id=deployment_id,
            executor_id=executor_id,
        )


@router.get(
    "/api/v1/local-model-catalog",
    response_model=LocalModelCatalogResponse,
)
async def search_local_model_catalog(
    request: Request,
    source: LocalModelCatalogSource | None = None,
    query: str = Query(default="", max_length=100),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=24),
    parameter_range: Literal["le4b", "4b_8b", "8b_14b", "14b_32b", "32b_70b", "70b_plus"]
    | None = None,
    download_size_range: Literal["le4gib", "4gib_8gib", "8gib_16gib", "16gib_32gib", "32gib_plus"]
    | None = None,
    quantization: str | None = Query(default=None, min_length=1, max_length=64),
    min_context: int | None = Query(default=None, ge=1024, le=2**31 - 1),
    include_unknown: bool = True,
) -> LocalModelCatalogResponse:
    require_current_user(request)
    catalog: LocalModelCatalog = request.app.state.local_model_catalog
    try:
        return await catalog.search(
            source=source,
            query=query,
            cursor=cursor,
            limit=limit,
            parameter_range=parameter_range,
            download_size_range=download_size_range,
            quantization=quantization,
            min_context=min_context,
            include_unknown=include_unknown,
        )
    except ValueError as exc:
        message = str(exc)
        raise api_exception(
            422,
            "validation_error",
            message,
            details=_validation_details(message),
        ) from exc


@router.get(
    "/api/v1/local-model-catalog/detail",
    response_model=LocalModelCatalogItem,
)
async def resolve_local_model_catalog_detail(
    request: Request,
    repo: str = Query(min_length=3, max_length=193),
    revision_sha: str | None = Query(default=None, min_length=40, max_length=40),
) -> LocalModelCatalogItem:
    """Resolve bounded public HF metadata for one visible or selected repository."""

    require_current_user(request)
    catalog: LocalModelCatalog = request.app.state.local_model_catalog
    try:
        return await catalog.detail(repo_id=repo, revision_sha=revision_sha)
    except ValueError as exc:
        message = str(exc)
        raise api_exception(
            422,
            "validation_error",
            message,
            details=_validation_details(message),
        ) from exc
    except CatalogUpstreamError as exc:
        raise api_exception(
            503,
            "huggingface_detail_unavailable",
            str(exc),
            details={"retry_after_seconds": exc.retry_after_seconds},
        ) from exc


@router.get(
    "/api/v1/local-model-catalog/resolve",
    response_model=LocalModelCatalogItem,
)
async def resolve_local_model_catalog_reference(
    request: Request,
    ref: str = Query(min_length=1, max_length=255),
) -> LocalModelCatalogItem:
    require_current_user(request)
    catalog: LocalModelCatalog = request.app.state.local_model_catalog
    try:
        return catalog.resolve_reference(ref)
    except ValueError as exc:
        raise api_exception(422, "validation_error", str(exc)) from exc


@router.post(
    "/api/v1/local-model-fit-plans",
    response_model=LocalModelFitPlanResponse,
)
async def create_local_model_fit_plan(
    request: Request,
    payload: LocalModelFitPlanRequest,
) -> LocalModelFitPlanResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        service = LocalModelDeploymentService(
            session,
            actor_email=user.email,
            actor_role=user.role,
        )
        try:
            rows = await service.resolve_selector(payload.selector)
            if payload.provider_id is not None:
                provider_host_ids = {
                    row.executor_id
                    for row in await service.resolve_provider_hosts(payload.provider_id)
                }
                requested_host_ids = {row.executor_id for row in rows}
                if not requested_host_ids.issubset(provider_host_ids):
                    raise LocalModelValidationError(
                        "requested fit hosts must be a subset of provider hosts"
                    )
        except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
            _translate_service_error(exc)
    return plan_local_model_fit(
        payload.model,
        context_tokens=payload.context_tokens,
        executors=[FitExecutor.from_row(row) for row in rows],
    )


@router.get(
    "/api/v1/local-model-deployments",
    response_model=list[LocalModelDeploymentResponse],
)
async def list_local_model_deployments(
    request: Request,
) -> list[LocalModelDeploymentResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        service = LocalModelDeploymentService(
            session,
            actor_email=user.email,
            actor_role=user.role,
        )
        rows = await service.list_deployments()
    return [_deployment_response(row) for row in rows]


@router.post(
    "/api/v1/local-model-deployments",
    response_model=LocalModelDeploymentResponse,
    status_code=201,
)
async def create_local_model_deployment(
    request: Request,
    payload: LocalModelDeploymentCreateRequest,
) -> LocalModelDeploymentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            row = await service.create_deployment(payload)
            await session.commit()
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    _trigger_reconcile(request, deployment_id=row.deployment_id)
    return _deployment_response(row)


@router.post(
    "/api/v1/local-model-deployments:managed",
    response_model=LocalModelManagedDeploymentCreateResponse,
    status_code=201,
)
async def create_managed_local_model_deployment(
    request: Request,
    payload: LocalModelManagedDeploymentCreateRequest,
) -> LocalModelManagedDeploymentCreateResponse:
    """Atomically reuse/create a compatible provider and create a deployment."""

    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            row, provider, created, reason = await service.create_managed_deployment(payload)
            await session.commit()
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    _trigger_reconcile(request, deployment_id=row.deployment_id)
    return LocalModelManagedDeploymentCreateResponse(
        deployment=_deployment_response(row),
        provider_id=provider.provider_id,
        provider_created=created,
        provider_reason_code=reason,
    )


@router.post(
    "/api/v1/local-model-deployments/{deployment_id}:attach-managed-provider",
    response_model=LocalModelManagedProviderAttachResponse,
)
async def attach_managed_local_model_provider(
    request: Request,
    deployment_id: str,
    payload: LocalModelManagedProviderAttachRequest,
) -> LocalModelManagedProviderAttachResponse:
    """Atomically create/reuse and attach a provider to a legacy deployment."""

    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            row, provider, created, reason = await service.attach_managed_provider(
                deployment_id,
                payload,
            )
            await session.commit()
    except (
        LocalModelAccessError,
        LocalModelDependencyError,
        LocalModelNotFoundError,
        LocalModelValidationError,
    ) as exc:
        _translate_service_error(exc)
    _trigger_reconcile(request, deployment_id=deployment_id)
    return LocalModelManagedProviderAttachResponse(
        deployment=_deployment_response(row),
        provider_id=provider.provider_id,
        provider_created=created,
        provider_reason_code=reason,
    )


@router.post(
    "/api/v1/local-model-providers/recommendations",
    response_model=LocalModelProviderRecommendationResponse,
)
async def recommend_local_model_provider(
    request: Request,
    payload: LocalModelProviderRecommendationRequest,
) -> LocalModelProviderRecommendationResponse:
    user = require_current_user(request)
    if payload.shared and user.role != "admin":
        raise api_exception(403, "forbidden", "shared provider selection requires an admin")
    from cognis.core.local_models import parse_local_model_reference

    try:
        parsed = parse_local_model_reference(payload.requested_ref)
        async with request.app.state.session_factory() as session:
            service = LocalModelProviderService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            recommendation = await service.recommend(
                runtime_name=parsed.runtime_name,
                selector=payload.selector,
                shared=payload.shared,
            )
            return recommendation.model_copy(update={"requested_ref": payload.requested_ref})
    except LookupError as exc:
        raise api_exception(404, "not_found", str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        raise api_exception(
            422,
            "validation_error",
            message,
            details=_validation_details(message),
        ) from exc


@router.post(
    "/api/v1/local-model-providers:find-or-create",
    response_model=LocalModelProviderFindOrCreateResponse,
)
async def find_or_create_local_model_provider(
    request: Request,
    payload: LocalModelProviderFindOrCreateRequest,
) -> LocalModelProviderFindOrCreateResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    if payload.selector is None:
        raise api_exception(422, "validation_error", "selector is required")
    from cognis.core.local_models import parse_local_model_reference

    try:
        parsed = parse_local_model_reference(payload.requested_ref)
        async with request.app.state.session_factory() as session:
            service = LocalModelProviderService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            provider, created, reason = await service.find_or_create(
                runtime_name=parsed.runtime_name,
                selector=payload.selector,
                shared=payload.shared,
                force_create=payload.force_create,
            )
            await session.commit()
    except PermissionError as exc:
        raise api_exception(403, "forbidden", str(exc)) from exc
    except LookupError as exc:
        raise api_exception(404, "not_found", str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        raise api_exception(
            422,
            "validation_error",
            message,
            details=_validation_details(message),
        ) from exc
    return LocalModelProviderFindOrCreateResponse(
        provider_id=provider.provider_id,
        created=created,
        reason_code=reason,
    )


@router.get(
    "/api/v1/local-model-deployments/{deployment_id}",
    response_model=LocalModelDeploymentResponse,
)
async def get_local_model_deployment(
    request: Request,
    deployment_id: str,
) -> LocalModelDeploymentResponse:
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            row = await service.get_deployment(deployment_id)
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    return _deployment_response(row)


@router.patch(
    "/api/v1/local-model-deployments/{deployment_id}",
    response_model=LocalModelDeploymentResponse,
)
async def update_local_model_deployment(
    request: Request,
    deployment_id: str,
    payload: LocalModelDeploymentUpdateRequest,
) -> LocalModelDeploymentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            row = await service.update_deployment(deployment_id, payload)
            await session.commit()
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    _trigger_reconcile(request, deployment_id=deployment_id)
    return _deployment_response(row)


@router.delete("/api/v1/local-model-deployments/{deployment_id}", status_code=204)
async def delete_local_model_deployment(
    request: Request,
    deployment_id: str,
) -> Response:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            await service.delete_deployment(deployment_id)
            await session.commit()
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    _trigger_reconcile(request)
    return Response(status_code=204)


@router.get(
    "/api/v1/local-model-deployments/{deployment_id}/targets",
    response_model=list[LocalModelTargetStatusResponse],
)
async def list_local_model_targets(
    request: Request,
    deployment_id: str,
) -> list[LocalModelTargetStatusResponse]:
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            rows = await service.list_targets(deployment_id)
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    return [_target_response(row) for row in rows]


@router.get(
    "/api/v1/local-model-deployments/{deployment_id}/status",
    response_model=LocalModelDeploymentStatusResponse,
)
async def get_local_model_deployment_status(
    request: Request,
    deployment_id: str,
) -> LocalModelDeploymentStatusResponse:
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            return await service.deployment_status(deployment_id)
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)


@router.get(
    "/api/v1/local-model-deployments/{deployment_id}/operations",
    response_model=list[LocalModelOperationResponse],
)
async def list_local_model_operations(
    request: Request,
    deployment_id: str,
) -> list[LocalModelOperationResponse]:
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            rows = await service.list_operations(deployment_id)
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    return [_operation_response(row) for row in rows]


@router.post(
    "/api/v1/local-model-deployments/{deployment_id}/reconciliation-requests",
    response_model=LocalModelDeploymentResponse,
    status_code=202,
)
async def request_local_model_reconciliation(
    request: Request,
    deployment_id: str,
) -> LocalModelDeploymentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            row = await service.request_reconciliation(deployment_id)
            await session.commit()
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    _trigger_reconcile(request, deployment_id=deployment_id)
    return _deployment_response(row)


@router.get(
    "/api/v1/executors/{executor_id}/local-model-runtime",
    response_model=OllamaRuntimeStatus,
)
async def get_executor_local_model_runtime(
    request: Request,
    executor_id: str,
) -> OllamaRuntimeStatus:
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            await service.authorize_executor(executor_id, mutation=False)
        return OllamaRuntimeStatus.model_validate(
            await request.app.state.local_model_runtime_manager.status(executor_id)
        )
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    except LocalModelRuntimeUnavailable as exc:
        raise api_exception(
            409,
            "local_model_runtime_unavailable",
            str(exc),
            details={"executor_id": executor_id},
        ) from exc


@router.post(
    "/api/v1/executors/{executor_id}/local-model-runtime/operations",
    response_model=LocalModelOperationResponse,
    status_code=202,
)
async def create_executor_local_model_operation(
    request: Request,
    executor_id: str,
    payload: LocalModelRuntimeOperationCreateRequest,
) -> LocalModelOperationResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            operation = await service.create_runtime_operation(executor_id, payload)
            await session.commit()
        await request.app.state.local_model_runtime_manager.dispatch(operation.operation_id)
    except (
        LocalModelAccessError,
        LocalModelDependencyError,
        LocalModelNotFoundError,
        LocalModelValidationError,
    ) as exc:
        _translate_service_error(exc)
    except LocalModelRuntimeUnavailable:
        pass
    async with request.app.state.session_factory() as session:
        refreshed = await session.get(type(operation), operation.operation_id)
        if refreshed is not None:
            operation = refreshed
    _trigger_reconcile(
        request,
        deployment_id=operation.deployment_id,
        executor_id=executor_id,
    )
    return _operation_response(operation)


@router.post(
    "/api/v1/executors/{executor_id}/local-model-runtime/operations/"
    "{operation_id}/cancellation-requests",
)
async def cancel_executor_local_model_operation(
    request: Request,
    executor_id: str,
    operation_id: str,
) -> dict[str, object]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            operation = await service.get_managed_runtime_operation(
                executor_id,
                operation_id,
            )
        result = cast(
            dict[str, Any],
            await request.app.state.local_model_runtime_manager.cancel(
                operation_id,
                executor_id=executor_id,
            ),
        )
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    _trigger_reconcile(
        request,
        deployment_id=operation.deployment_id,
        executor_id=executor_id,
    )
    return {
        "acknowledged": bool(result.get("acknowledged", False)),
        "rollback_guaranteed": False,
    }


@router.post(
    "/api/v1/llm-providers/{provider_id}/local-models:upsert",
    response_model=LLMProviderResponse,
)
async def upsert_provider_local_model(
    request: Request,
    provider_id: str,
    payload: ProviderLocalModelUpsertRequest,
) -> LLMProviderResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        async with request.app.state.session_factory() as session:
            service = LocalModelDeploymentService(
                session,
                actor_email=user.email,
                actor_role=user.role,
            )
            row = await service.upsert_provider_model(provider_id, payload)
            await session.commit()
    except (LocalModelAccessError, LocalModelNotFoundError, LocalModelValidationError) as exc:
        _translate_service_error(exc)
    return llm_provider_to_response(row)
