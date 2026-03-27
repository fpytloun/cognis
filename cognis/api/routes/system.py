"""System and health routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from cognis.api.models import HealthResponse

router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    providers = await request.app.state.providers.health()
    status = "healthy"
    if any(item.status not in {"healthy", "unknown"} for item in providers.values()):
        status = "degraded"
    return HealthResponse(
        status=status,
        providers={name: provider.model_dump() for name, provider in providers.items()},
        remember_queue={"depth": len(request.app.state.remember_queue._items)},
    )


@router.get("/api/health/providers")
async def health_providers(request: Request) -> dict[str, object]:
    providers = await request.app.state.providers.health()
    return {name: provider.model_dump() for name, provider in providers.items()}


@router.get("/api/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> JSONResponse:
    return JSONResponse(content=request.app.state.auth_provider.jwks())
