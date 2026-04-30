"""System and health routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from cognis.api.common import api_exception, require_admin
from cognis.api.models import HealthResponse, SystemDiagnosticsResponse
from cognis.store.queries import list_agents, list_llm_providers

router = APIRouter()


def _database_summary(database_url: str) -> dict[str, str | None]:
    parsed = make_url(database_url)
    return {
        "drivername": parsed.drivername,
        "database": parsed.database,
        "host": parsed.host,
        "port": str(parsed.port) if parsed.port is not None else None,
    }


async def _migration_version(request: Request) -> str | None:
    async with request.app.state.engine.connect() as conn:
        try:
            result = await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        except Exception:
            return None
    row = result.first()
    return str(row[0]) if row else None


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


@router.get("/api/v1/system/diagnostics", response_model=SystemDiagnosticsResponse)
async def diagnostics(request: Request) -> SystemDiagnosticsResponse:
    require_admin(request)
    config = request.app.state.config
    provider_health = await request.app.state.providers.health()
    async with request.app.state.session_factory() as session:
        agents = await list_agents(session)
        providers = await list_llm_providers(session)
        from cognis.store.queries import list_executors

        all_executors = await list_executors(session)
        executor_has_tools = any(
            (executor.enabled_tools and len(executor.enabled_tools) > 0)
            or (executor.enabled_tool_groups and len(executor.enabled_tool_groups) > 0)
            for executor in all_executors
        )

    provider_test_results: dict[str, Any] = getattr(request.app.state, "provider_test_results", {})
    provider_rows = []
    for provider in providers:
        provider_rows.append(
            {
                "provider_id": provider.provider_id,
                "display_name": provider.display_name,
                "location": provider.location,
                "backend": provider.backend,
                "status": provider.status,
                "models": list(provider.config.get("models", []))
                if isinstance(provider.config.get("models", []), list)
                else [],
                "last_test": provider_test_results.get(provider.provider_id),
            }
        )

    return SystemDiagnosticsResponse(
        readiness={
            "mnemory_reachable": provider_health["memory"].status == "healthy",
            "intaris_reachable": provider_health["guardrails"].status == "healthy",
            "llm_provider_configured": len(providers) > 0,
            "executor_tools_configured": executor_has_tools,
            "agent_created": len(agents) > 0,
            "chat_ready": len(providers) > 0 and len(agents) > 0,
        },
        ui={
            "enabled": bool(config.serve_ui),
            "assets_present": request.app.state.ui_build_dir is not None,
            "user_facing_url": request.app.state.user_facing_url,
        },
        database={
            **_database_summary(config.database_url),
            "migration_version": await _migration_version(request),
        },
        config={
            "data_dir": str(config.data_dir),
            "host": config.host,
            "port": config.port,
            "serve_ui": config.serve_ui,
            "mnemory_url": config.mnemory_url,
            "intaris_url": config.intaris_url,
            "public_mnemory_ui_url": config.public_mnemory_ui_url,
            "public_intaris_ui_url": config.public_intaris_ui_url,
            "log_level": config.log_level,
            "log_format": config.log_format,
            "cors_origins": config.cors_origins,
        },
        providers=provider_rows,
        agents={
            "count": len(agents),
            "names": [agent.name for agent in agents[:20]],
        },
        key_fingerprint=request.app.state.jwt_public_key_fingerprint,
    )


@router.get("/api/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/api/v1/system/invariants")
async def system_invariants(request: Request) -> dict[str, Any]:
    """Admin-only read-only probe of cross-subsystem invariants.

    Surfaces any persistent state drift that the runtime has not
    reconciled automatically. Useful for dashboards and alerts; any
    non-zero count should be investigated.
    """

    require_admin(request)
    from cognis.core.invariants import check_invariants

    async with request.app.state.session_factory() as session:
        reports = await check_invariants(session)
    return {
        "invariants": [report.as_dict() for report in reports],
        "startup_reports": getattr(request.app.state, "startup_invariant_reports", []),
    }


@router.post("/api/v1/system/reconcile")
async def system_reconcile(request: Request) -> dict[str, Any]:
    """Admin-only on-demand invariant reconciliation.

    Runs the same reconciler as controller startup. Idempotent — if the
    system is already consistent, repeat calls return zero counts.
    """

    require_admin(request)
    from cognis.core.invariants import reconcile_invariants

    async with request.app.state.session_factory() as session:
        reports = await reconcile_invariants(session)
    return {"invariants": [report.as_dict() for report in reports]}


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> JSONResponse:
    return JSONResponse(content=request.app.state.auth_provider.jwks())


@router.get("/.well-known/agent.json")
async def default_agent_card() -> JSONResponse:
    raise api_exception(
        501,
        "not_implemented",
        "Default public agent card requires additional discovery metadata and is deferred in MVP.",
    )
