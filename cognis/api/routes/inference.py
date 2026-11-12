"""OpenAI-compatible inference transport and companion credential administration."""

from __future__ import annotations

import asyncio
import base64
import logging
import struct
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from cognis import json_codec as json
from cognis.api.common import require_admin, require_current_user
from cognis.api.inference_usage import observe_error, observe_outcome, observe_response
from cognis.providers.llm.inference_accounting import attribution_from_headers
from cognis.providers.llm.inference_api import (
    InferenceAPIService,
    InferenceError,
    InferencePrincipal,
)
from cognis.providers.llm.inference_errors import safe_inference_error
from cognis.providers.llm.systemone import SystemOneRequest, SystemOneResponse
from cognis.providers.llm.usage_binding import USAGE_REQUEST_KEY
from cognis.providers.llm.usage_proxy import proxy_request
from cognis.store.models import InferenceCompanion

router = APIRouter(tags=["inference"])
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=512)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    stream: bool = False
    stream_options: dict[str, bool] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=131072)
    reasoning_effort: str | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=128)
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    n: Literal[1] = 1


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=512)
    input: str | list[str]
    dimensions: int | None = Field(default=None, ge=1, le=65536)
    encoding_format: Literal["float", "base64"] = "float"


def principal_for(request: Request) -> InferencePrincipal:
    user = require_current_user(request)
    scopes = getattr(request.state, "api_key_scopes", {})
    models = scopes.get("models")
    operations = scopes.get("operations")
    if models is not None and not (
        isinstance(models, list) and all(isinstance(item, str) for item in models)
    ):
        raise InferenceError(403, "permission_denied", "Invalid API key model policy")
    if operations is not None and not (
        isinstance(operations, list) and all(isinstance(item, str) for item in operations)
    ):
        raise InferenceError(403, "permission_denied", "Invalid API key operation policy")
    client_id = getattr(request.state, "inference_client_id", "browser_session")
    return InferencePrincipal(
        owner=user.email,
        client_id=client_id,
        auth_type=user.auth_type,
        service=client_id if user.auth_type == "inference_companion" else None,
        models=frozenset(models) if models is not None else None,
        operations=frozenset(operations) if operations is not None else None,
        token_id=getattr(request.state, "claims", {}).get("jti"),
    )


def service_for(request: Request) -> InferenceAPIService:
    return cast(InferenceAPIService, request.app.state.inference_api)


def failure(exc: InferenceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "error": {
                "message": str(exc),
                "type": "invalid_request_error",
                "param": None,
                "code": exc.code,
            }
        },
    )


def upstream_failure(exc: Exception) -> JSONResponse:
    """Expose only allowlisted metadata, distinguishing request failures from outages."""
    safe = safe_inference_error(exc)
    status = safe["upstream_status"]
    if safe["error_category"] == "request" or status in {400, 422}:
        code = safe["upstream_code"] or "request_error"
        return JSONResponse(
            status_code=422,
            content={"error": {"code": f"upstream_{code}", "category": "request"}},
        )
    return failure(InferenceError(502, "upstream_error", "Inference provider request failed"))


@router.get("/v1/models")
async def models(request: Request) -> Any:
    try:
        return {"object": "list", "data": await service_for(request).models(principal_for(request))}
    except InferenceError as exc:
        return failure(exc)


@router.get("/v1/models/{model_id:path}")
async def retrieve_model(request: Request, model_id: str, operation: str | None = None) -> Any:
    try:
        return await service_for(request).model(principal_for(request), model_id, operation)
    except InferenceError as exc:
        return failure(exc)
    except ValueError:
        return failure(InferenceError(404, "model_not_found", "Model unavailable for this caller"))


@router.post("/v1/chat/completions")
async def chat(request: Request, payload: ChatRequest) -> Any:
    service = service_for(request)
    try:
        principal = principal_for(request)
        target = await service.resolve(principal, payload.model, "chat")
    except InferenceError as exc:
        return failure(exc)
    except ValueError:
        return failure(InferenceError(400, "invalid_model", "Unknown or unavailable model"))
    kwargs = payload.model_dump(
        exclude_none=True,
        exclude={
            "model",
            "messages",
            "stream",
            "stream_options",
            "n",
        },
    )
    if target.reasoning_effort and "reasoning_effort" not in kwargs:
        kwargs["reasoning_effort"] = target.reasoning_effort
    # "default" explicitly opts out of route effort without sending a
    # Cognis-only value to upstream APIs. "none" remains an explicit disable.
    if kwargs.get("reasoning_effort") == "default":
        kwargs.pop("reasoning_effort")
    kwargs.update(
        provider_id=None if target.route_reference else target.provider_id,
        acting_user_email=target.owner,
    )
    proxy_identity = (
        request.headers.get("X-Cognis-Session-ID")
        or request.headers.get("X-Cognis-Client-ID")
        or principal.client_id
    )
    if len(proxy_identity) > 256:
        return failure(InferenceError(400, "invalid_request", "Session ID is too long"))
    import hashlib

    kwargs["cognis_session_id"] = (
        "proxy-" + hashlib.sha256(f"{principal.client_id}\0{proxy_identity}".encode()).hexdigest()
    )
    kwargs["timeout"] = 3.5 if target.requested == "routes/intaris_evaluation" else 120
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    kwargs["cognis_llm_request_id"] = request_id
    kwargs[USAGE_REQUEST_KEY] = proxy_request(
        request,
        service,
        principal,
        target,
        operation="chat",
        parameters={**kwargs, "_usage_streaming": payload.stream},
    )
    started = time.monotonic()
    usage: dict[str, Any] = {}
    if not payload.stream:
        outcome = "error"
        try:
            async with service.admission(principal):
                response = await service.llm.generate(
                    payload.messages,
                    model=target.route_reference or target.model,
                    preserve_response_format=True,
                    **kwargs,
                )
            from dataclasses import replace

            actual = response.pop("_cognis_inference", {})
            if actual:
                target = replace(target, **actual)
            usage = response.get("usage") or {}
            observe_response(request, response)
            outcome = "success"
            response = dict(response)
            response.update(id=request_id, object="chat.completion", model=payload.model)
            response.setdefault("created", int(time.time()))
            # Internal transport/performance telemetry is not part of OpenAI's contract.
            for key in ("performance", "response_status"):
                response.pop(key, None)
            return JSONResponse(response, headers={"x-request-id": request_id})
        except InferenceError as exc:
            return failure(exc)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            observe_error(request, exc)
            logger.warning(
                "inference_provider_error",
                extra={"extra_data": {"request_id": request_id, **safe_inference_error(exc)}},
            )
            response = upstream_failure(exc)
            response.headers["x-request-id"] = request_id
            return response
        finally:
            observe_outcome(request, outcome)
            service.observe(
                principal,
                target,
                "chat",
                request_id,
                started,
                outcome,
                usage,
                attribution_from_headers(request.headers),
            )

    # Check before headers, but acquire inside the iterator so an immediate
    # disconnect cannot strand a capacity slot before iteration starts.
    # Request provider usage even when the caller elects not to receive it.
    kwargs["stream_options"] = {"include_usage": True}
    try:
        service.check_admission(principal)
    except InferenceError as exc:
        return failure(exc)

    async def chunks() -> AsyncIterator[str]:
        nonlocal usage
        outcome = "error"
        serving_target = target
        stream = service.llm.stream_generate(
            payload.messages,
            model=target.route_reference or target.model,
            cognis_emit_selection=True,
            **kwargs,
        )
        try:
            async with service.admission(principal):
                async for chunk in stream:
                    observe_response(request, chunk)
                    if "_inference_selection" in chunk:
                        from dataclasses import replace

                        selected = chunk["_inference_selection"]
                        serving_target = replace(
                            target, provider_id=selected["provider_id"], model=selected["model"]
                        )
                        continue
                    if await request.is_disconnected():
                        outcome = "cancelled"
                        return
                    if isinstance(chunk.get("usage"), dict):
                        usage = dict(chunk["usage"])
                    if (
                        chunk.get("error")
                        or chunk.get("response_error")
                        or chunk.get("mid_stream_failure")
                    ):
                        yield 'data: {"error":{"message":"Inference provider stream failed","type":"upstream_error"}}\n\n'
                        yield "data: [DONE]\n\n"
                        return
                    wire = {key: chunk[key] for key in ("choices", "usage") if key in chunk}
                    if "usage" in wire and not (payload.stream_options or {}).get("include_usage"):
                        wire.pop("usage")
                    if not wire.get("choices") and "usage" not in wire:
                        continue
                    wire.update(
                        id=request_id,
                        object="chat.completion.chunk",
                        created=int(time.time()),
                        model=payload.model,
                    )
                    wire.setdefault("choices", [])
                    yield f"data: {json.dumps(wire)}\n\n"
            outcome = "success"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            observe_error(request, exc)
            yield f"data: {bytes(upstream_failure(exc).body).decode()}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            try:
                await stream.aclose()
            finally:
                observe_outcome(request, outcome)
                service.observe(
                    principal,
                    serving_target,
                    "chat",
                    request_id,
                    started,
                    outcome,
                    usage,
                    attribution_from_headers(request.headers),
                )

    return StreamingResponse(
        chunks(),
        media_type="text/event-stream",
        headers={
            "x-request-id": request_id,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/v1/systemone", response_model=SystemOneResponse)
async def systemone(request: Request, payload: SystemOneRequest) -> Any:
    """Proxy typed decisions under the same scoped inference identity."""
    service = service_for(request)
    try:
        principal = principal_for(request)
        target = await service.resolve(principal, payload.model, "systemone")
    except InferenceError as exc:
        return failure(exc)
    except ValueError:
        return failure(InferenceError(400, "invalid_model", "Unknown or unavailable model"))
    request_id = f"systemone-{uuid.uuid4().hex}"
    scope = proxy_request(
        request,
        service,
        principal,
        target,
        operation="systemone",
        parameters={"cognis_llm_request_id": request_id},
    )
    started = time.monotonic()
    outcome = "error"
    usage: dict[str, Any] = {}
    try:
        async with service.admission(principal):
            response = await service.llm.system_one(
                payload.model_copy(update={"model": target.route_reference or target.model}),
                provider_id=None if target.route_reference else target.provider_id,
                acting_user_email=target.owner,
                timeout=3.5 if target.requested == "routes/intaris_evaluation" else 120,
                **{USAGE_REQUEST_KEY: scope},
            )
        from dataclasses import replace

        actual = response.pop("_cognis_inference", {})
        if actual:
            target = replace(target, **actual)
        usage = response.get("usage") or {}
        outcome = "success"
        return JSONResponse(response, headers={"x-request-id": request_id})
    except InferenceError as exc:
        return failure(exc)
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:
        observe_error(request, exc)
        logger.warning(
            "inference_provider_error",
            extra={
                "extra_data": {
                    "request_id": request_id,
                    "operation": "systemone",
                    "provider_id": target.provider_id,
                    "model": target.model,
                    **safe_inference_error(exc),
                }
            },
        )
        response = upstream_failure(exc)
        response.headers["x-request-id"] = request_id
        return response
    finally:
        observe_outcome(request, outcome)
        service.observe(
            principal,
            target,
            "systemone",
            request_id,
            started,
            outcome,
            usage,
            attribution_from_headers(request.headers),
        )


@router.post("/v1/embeddings")
async def embeddings(request: Request, payload: EmbeddingRequest) -> Any:
    texts = [payload.input] if isinstance(payload.input, str) else payload.input
    if not texts or len(texts) > 2048:
        return failure(InferenceError(400, "invalid_input", "Provide 1–2048 text inputs"))
    service = service_for(request)
    try:
        principal = principal_for(request)
        target = await service.resolve(principal, payload.model, "embeddings")
    except InferenceError as exc:
        return failure(exc)
    except ValueError:
        return failure(InferenceError(400, "invalid_model", "Unknown or unavailable model"))
    request_id = f"embd-{uuid.uuid4().hex}"
    started = time.monotonic()
    usage: dict[str, Any] = {}
    outcome = "error"
    kwargs: dict[str, Any] = {
        "provider_id": target.provider_id,
        "acting_user_email": target.owner,
    }
    if payload.dimensions:
        kwargs["dimensions"] = payload.dimensions
    kwargs["cognis_llm_request_id"] = request_id
    kwargs[USAGE_REQUEST_KEY] = proxy_request(
        request,
        service,
        principal,
        target,
        operation="embeddings",
        parameters=kwargs,
    )
    try:
        async with service.admission(principal):
            response = await service.llm.embed_response(texts, model=target.model, **kwargs)
        serving = response.pop("_cognis_inference", None)
        if serving and (
            serving.get("provider_id") != target.provider_id or serving.get("model") != target.model
        ):
            raise InferenceError(409, "embedding_space_changed", "Embedding target changed")
        usage = response.get("usage") or {}
        data = response.get("data") or []
        if len(data) != len(texts) or not all(
            isinstance(item.get("embedding"), list) and item["embedding"] for item in data
        ):
            raise InferenceError(502, "upstream_error", "Invalid embedding provider response")
        dimensions = len(data[0]["embedding"])
        if any(len(item["embedding"]) != dimensions for item in data):
            raise InferenceError(502, "upstream_error", "Inconsistent embedding dimensions")
        await service.bind_embedding(principal, target, dimensions)
        if payload.encoding_format == "base64":
            for item in response["data"]:
                vector = item["embedding"]
                item["embedding"] = base64.b64encode(
                    struct.pack(f"<{len(vector)}f", *vector)
                ).decode("ascii")
        response["model"] = payload.model
        outcome = "success"
        return JSONResponse(response, headers={"x-request-id": request_id})
    except InferenceError as exc:
        return failure(exc)
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:
        observe_error(request, exc)
        return upstream_failure(exc)
    finally:
        observe_outcome(request, outcome)
        service.observe(
            principal,
            target,
            "embeddings",
            request_id,
            started,
            outcome,
            usage,
            attribution_from_headers(request.headers),
        )


@router.post("/api/v1/inference/companions/{service}/token")
async def companion_token(request: Request, service: Literal["intaris", "mnemory"]) -> Any:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        row = (
            await session.execute(
                select(InferenceCompanion)
                .where(InferenceCompanion.service == service)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            row = InferenceCompanion(service=service, token_version=0, enabled=True)
            session.add(row)
        else:
            row.token_version += 1
            row.enabled = True
        await session.commit()
        version = row.token_version
    token = request.app.state.auth_provider.sign_inference_companion_token(service, version)
    directory = request.app.state.config.inference_credential_dir
    if directory is not None:
        from cognis.providers.auth.inference_credentials import write_companion_token

        write_companion_token(directory, service, token)
    return {
        "service": service,
        "token": token,
    }


@router.delete("/api/v1/inference/companions/{service}/token")
async def revoke_companion(request: Request, service: Literal["intaris", "mnemory"]) -> Any:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        await session.execute(
            update(InferenceCompanion)
            .where(InferenceCompanion.service == service)
            .values(enabled=False, token_version=InferenceCompanion.token_version + 1)
        )
        await session.commit()
    return {"service": service, "revoked": True}


class ResetEmbeddingBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reindex_migration_prepared: Literal[True]


@router.post("/api/v1/inference/companions/{service}/reset-embedding")
async def reset_embedding(
    request: Request,
    service: Literal["intaris", "mnemory"],
    payload: ResetEmbeddingBinding,
) -> Any:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        await session.execute(
            update(InferenceCompanion)
            .where(InferenceCompanion.service == service)
            .values(embedding_reference=None, embedding_dimensions=None)
        )
        await session.commit()
    return {"service": service, "embedding_binding_reset": True}
