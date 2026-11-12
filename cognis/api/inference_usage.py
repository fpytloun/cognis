"""Authenticated inference request binding and strictly allowlisted HTTP metadata."""

from __future__ import annotations

import re
from typing import Any, Literal, cast

from starlette.requests import Request

from cognis.models.usage import UsageAttribution, UsageHTTPMetadata, UsageIdentity
from cognis.providers.llm.usage_delivery import bind_usage_request
from cognis.providers.llm.usage_normalization import safe_error
from cognis.providers.llm.usage_proxy import bound_proxy_metadata, proxy_client_labels

INFERENCE_PATHS = frozenset({"/v1/chat/completions", "/v1/embeddings", "/v1/systemone"})


def bind_authenticated_inference(request: Request) -> None:
    """Bind before validation/admission without reading or retaining request bodies."""
    if request.method != "POST" or request.url.path not in INFERENCE_PATHS:
        return
    service = getattr(request.app.state, "inference_api", None)
    capture = getattr(getattr(service, "llm", None), "usage_capture", None)
    if capture is None:
        return
    user = request.state.user
    client = getattr(request.state, "inference_client_id", "browser_session")
    companion = client if user.auth_type == "inference_companion" else None
    label, labels = proxy_client_labels(request)
    scope = capture.request(
        attribution=UsageAttribution(
            identity=UsageIdentity(
                traffic_origin="api",
                principal_type="service" if companion else "user",
                principal_subject=companion or user.email,
                usage_subject_kind="service" if companion else "user",
                usage_subject_id=companion or user.email,
                resource_owner=user.email,
                initiating_actor=None if companion else user.email,
                client_id=client,
                key_record_id=getattr(request.state, "api_key_id", None),
                auth_type=user.auth_type,
                service_id=companion,
            ),
            purpose=label or "inference_api",
            client_labels=labels,
        )
    )
    fields: dict[str, Any] = {"request_path": request.url.path}
    if raw := request.headers.get("user-agent"):
        fields["user_agent"] = " ".join(
            "".join(char for char in raw[:1024] if 32 <= ord(char) <= 126).split()
        )[:256]
    for field in ("app_name", "app_version"):
        value = request.headers.get("x-cognis-inference-" + field.replace("_", "-"))
        if value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", value):
            fields[field] = value
    scope.http_metadata = UsageHTTPMetadata.model_validate(fields)
    bound_proxy_metadata(scope)
    bind_usage_request(request.scope, scope)


def observe_response(request: Request, response: dict[str, Any]) -> None:
    """Retain only the normalized first choice's finish reason, never its content."""
    scope = request.scope.get("state", {}).get("cognis_usage_request")
    if scope is None or scope.http_metadata is None:
        return
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str) and reason:
            scope.http_metadata = scope.http_metadata.model_copy(
                update={
                    "finish_reason": reason
                    if reason in {"stop", "length", "tool_calls", "function_call", "content_filter"}
                    else "other"
                }
            )


def observe_outcome(request: Request, outcome: str) -> None:
    """Explicit generator outcomes survive an otherwise successful SSE transport."""
    scope = request.scope.get("state", {}).get("cognis_usage_request")
    if scope is not None and outcome in {"success", "error", "cancelled"}:
        scope.gateway_outcome = cast(Literal["success", "error", "cancelled"], outcome)


def observe_error(request: Request, error: BaseException) -> None:
    """Store fixed error templates, never an exception message."""
    scope = request.scope.get("state", {}).get("cognis_usage_request")
    if scope is not None and scope.http_metadata is not None:
        scope.http_metadata = scope.http_metadata.model_copy(update={"error": safe_error(error)})
