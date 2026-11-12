"""Authenticated gateway subjects; client labels do not confer resource authority."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from starlette.requests import Request

from cognis.models.usage import UsageAttribution, UsageClientLabels, UsageIdentity
from cognis.providers.llm.usage_capture import UsageCapture, UsageRequest, optional_id
from cognis.providers.llm.usage_delivery import bind_usage_request
from cognis.providers.llm.usage_parameters import project_parameters

COMPANION_OPERATIONS = frozenset(
    {
        "summary_window",
        "compaction",
        "l3",
        "intention",
        "source_context",
        "policy_compile",
        "policy_assess",
        "alignment",
        "evaluation",
        "judge",
        "search_embedding",
        "unknown",
        "extract",
        "remember_extract",
        "remember_dedup",
        "compact",
        "shorten",
        "reclassify",
        "evidence_semantic_equivalence",
        "classify",
        "query_gen",
        "rerank",
        "answer",
        "consolidation",
        "fsck_security",
        "fsck_dedup",
        "fsck_content_quality",
        "fsck_metadata_normalization",
        "embed",
        "embed_batch",
    }
)


def proxy_client_labels(request: Request) -> tuple[str | None, UsageClientLabels]:
    """Parse correlation before resolution; these labels never establish authority."""
    claimed = request.headers.get("x-cognis-inference-operation")
    label = claimed if claimed in COMPANION_OPERATIONS else None
    values = {
        field: value
        for field, header in (
            ("operation_id", "x-cognis-inference-operation-id"),
            ("session_id", "x-cognis-inference-session-id"),
            ("task_id", "x-cognis-inference-task-id"),
            ("client_request_id", "x-cognis-inference-client-request-id"),
        )
        if (value := optional_id(request.headers.get(header))) is not None
    }
    try:
        return label, UsageClientLabels(operation=label, **values)
    except ValidationError:
        return None, UsageClientLabels(**values)


def bound_proxy_metadata(scope: UsageRequest) -> None:
    """Reserve room for terminal safe errors in a 2-KiB private metadata budget."""
    metadata, labels = scope.http_metadata, scope.attribution.client_labels
    if (
        metadata is not None
        and labels is not None
        and (
            len(metadata.model_dump_json(exclude_none=True).encode())
            + len(labels.model_dump_json(exclude_none=True).encode())
            > 1024
        )
    ):
        scope.attribution = scope.attribution.model_copy(update={"client_labels": None})
        scope.capture.reduced_contexts += 1


def proxy_request(
    request: Request,
    service: Any,
    principal: Any,
    target: Any,
    *,
    operation: str,
    parameters: dict[str, Any],
) -> UsageRequest | None:
    """Bind authenticated identity; caller headers never bind resources."""
    capture: UsageCapture | None = service.llm.usage_capture
    if capture is None:
        return None
    companion = principal.service
    label, labels = proxy_client_labels(request)
    identity = UsageIdentity(
        traffic_origin="api",
        principal_type="service" if companion else "user",
        principal_subject=companion or principal.owner,
        usage_subject_kind="service" if companion else "user",
        usage_subject_id=companion or principal.owner,
        resource_owner=principal.owner,
        initiating_actor=None if companion else principal.owner,
        provider_payer=target.owner,
        service_id=companion,
        client_id=optional_id(principal.client_id),
        auth_type=optional_id(principal.auth_type),
        service_operation=label,
        key_record_id=optional_id(getattr(request.state, "api_key_id", None)),
        token_record_id=optional_id(principal.token_id),
    )
    existing = request.scope.get("state", {}).get("cognis_usage_request")
    scope = existing or capture.request(
        attribution=UsageAttribution(
            identity=identity,
            purpose=label or operation,
            client_labels=labels,
        ),
        requested_model=target.requested,
        requested_parameters=project_parameters(parameters),
        request_id=optional_id(parameters.get("cognis_llm_request_id")),
    )
    if existing is not None:
        scope.request_id = optional_id(parameters.get("cognis_llm_request_id")) or scope.request_id
        scope.attribution = UsageAttribution(
            identity=identity, purpose=label or operation, client_labels=labels
        )
        scope.requested_model = target.requested
        scope.requested_parameters = project_parameters(parameters)
    if scope.http_metadata is not None:
        scope.http_metadata = scope.http_metadata.model_copy(
            update={"streaming": bool(parameters.get("_usage_streaming", False))}
        )
    bind_usage_request(request.scope, scope)
    bound_proxy_metadata(scope)
    return scope
