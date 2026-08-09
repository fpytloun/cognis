"""System and health routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from cognis import __version__
from cognis.api.common import api_exception, require_admin
from cognis.api.models import (
    ClientDiscoveryCapabilities,
    ClientDiscoveryPaths,
    ClientDiscoveryProduct,
    ClientDiscoveryProtocol,
    ClientDiscoveryResponse,
    ClientDiscoveryServer,
    HealthResponse,
    ResolveAmbiguousDirectTurnRequest,
    ResolveAmbiguousDirectTurnResponse,
    StaleDirectTurnResponse,
    SystemDiagnosticsResponse,
)
from cognis.core.controller_runtime import ControllerLifecycleState
from cognis.store.database import check_connection
from cognis.store.direct_turns import (
    DirectTurnRecoveryConflict,
    DirectTurnRecoverySnapshot,
    DirectTurnStore,
)
from cognis.store.queries import list_agents, list_llm_providers

router = APIRouter()
logger = logging.getLogger(__name__)

PWA_RESET_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta http-equiv="Cache-Control" content="no-store" />
    <title>Reset Cognis app cache</title>
    <style>
      :root { color-scheme: dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #020617; color: #e2e8f0; }
      main { max-width: 34rem; padding: 2rem; }
      h1 { margin: 0 0 0.75rem; font-size: 1.5rem; }
      p { color: #94a3b8; line-height: 1.5; }
      a { color: #7dd3fc; }
      code { color: #fbbf24; }
    </style>
  </head>
  <body>
    <main>
      <h1>Resetting Cognis app cache…</h1>
      <p id="status">Clearing the installed PWA shell and reloading Cognis from the network.</p>
      <p>If this page does not continue automatically, <a href="/">open Cognis</a>.</p>
    </main>
    <script>
      (async () => {
        const status = document.getElementById('status');
        try {
          if ('serviceWorker' in navigator) {
            const registrations = await navigator.serviceWorker.getRegistrations();
            await Promise.all(registrations.map((registration) => registration.unregister()));
          }
          if ('caches' in window) {
            const names = await caches.keys();
            await Promise.all(
              names
                .filter((name) => name.startsWith('cognis-'))
                .map((name) => caches.delete(name))
            );
          }
          location.replace('/?pwa-reset=' + Date.now());
        } catch (error) {
          console.error(error);
          if (status) {
            status.innerHTML = 'Automatic reset failed. Close this app window, reopen Cognis in a browser, and use the link below.';
          }
        }
      })();
    </script>
  </body>
</html>
"""


def _optional_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _stale_turn_response(row: Any) -> StaleDirectTurnResponse:
    outcome = row.outcome if isinstance(row.outcome, dict) else {}
    raw_call_ids = outcome.get("call_ids")
    call_ids = (
        [item for item in raw_call_ids if isinstance(item, str)][:100]
        if isinstance(raw_call_ids, list)
        else []
    )
    call_id = outcome.get("call_id")
    timeout = outcome.get("tool_timeout_seconds", outcome.get("timeout_seconds"))
    return StaleDirectTurnResponse(
        request_id=row.request_id,
        conversation_id=row.conversation_id,
        owner_controller_id=row.owner_controller_id,
        owner_incarnation_id=row.owner_incarnation_id,
        fencing_token=row.fencing_token,
        status=row.status,
        phase=str(outcome.get("phase") or ""),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        phase_started_at=_optional_datetime(outcome.get("phase_started_at")),
        call_id=call_id if isinstance(call_id, str) else None,
        call_ids=call_ids,
        timeout_seconds=float(timeout) if isinstance(timeout, int | float) else None,
    )


@router.get(
    "/api/v1/system/direct-turns/stale",
    response_model=dict[str, Any],
)
async def list_stale_direct_turns(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    """List stale active direct turns without exposing durable payload content."""
    require_admin(request)
    try:
        after = int(cursor) if cursor is not None else 0
    except ValueError as exc:
        raise api_exception(422, "invalid_cursor", "Cursor must be an admission order") from exc
    if after < 0:
        raise api_exception(422, "invalid_cursor", "Cursor must be non-negative")
    store = DirectTurnStore(request.app.state.session_factory)
    rows, has_more = await store.list_stale_active_page(
        after_admission_order=after,
        limit=limit,
    )
    return {
        "items": [_stale_turn_response(row).model_dump(mode="json") for row in rows],
        "cursor": str(rows[-1].admission_order) if has_more and rows else None,
        "has_more": has_more,
    }


@router.post(
    "/api/v1/system/direct-turns/{request_id}/resolve-ambiguous",
    response_model=ResolveAmbiguousDirectTurnResponse,
)
async def resolve_ambiguous_direct_turn(
    request: Request,
    request_id: str,
    payload: ResolveAmbiguousDirectTurnRequest,
) -> ResolveAmbiguousDirectTurnResponse:
    """Fence and quarantine one stale uncertain tool effect."""
    admin = require_admin(request)
    store = DirectTurnStore(request.app.state.session_factory)
    try:
        result = await store.resolve_stale_tool_ambiguous(
            request_id,
            actor_email=admin.email,
            reason=payload.reason,
            client_transaction_id=payload.client_transaction_id,
            expected=DirectTurnRecoverySnapshot(
                conversation_id=payload.conversation_id,
                status=payload.status,
                phase=payload.phase,
                owner_controller_id=payload.owner_controller_id,
                owner_incarnation_id=payload.owner_incarnation_id,
                fencing_token=payload.fencing_token,
                updated_at=payload.updated_at,
                phase_started_at=payload.phase_started_at,
            ),
        )
    except DirectTurnRecoveryConflict as exc:
        status_code = 404 if exc.code == "not_found" else 409
        raise api_exception(status_code, exc.code, str(exc)) from exc
    if result.changed:
        try:
            await request.app.state.turn_scheduler.wake_direct_turn_runtime()
        except Exception:
            logger.warning(
                "direct-turn operator recovery committed but wake failed",
                exc_info=True,
                extra={
                    "request_id": result.request_id,
                    "conversation_id": result.conversation_id,
                },
            )
    return ResolveAmbiguousDirectTurnResponse(
        request_id=result.request_id,
        conversation_id=result.conversation_id,
        status="ambiguous",
        phase="ambiguous",
        fencing_token=result.fencing_token,
        changed=result.changed,
    )


def _database_summary(database_url: str) -> dict[str, str | None]:
    parsed = make_url(database_url)
    return {
        "drivername": parsed.drivername,
        "database": parsed.database,
        "host": parsed.host,
        "port": str(parsed.port) if parsed.port is not None else None,
    }


def _redis_diagnostics(request: Request) -> dict[str, bool]:
    redis_service = getattr(request.app.state, "redis_service", None)
    session_cache = getattr(request.app.state, "session_cache", None)
    event_cache = getattr(request.app.state, "cached_event_store", None)
    runtime_relay = getattr(request.app.state, "chat_v2_runtime_relay", None)
    return {
        "configured": bool(getattr(redis_service, "configured", False)),
        "available": bool(getattr(redis_service, "available", False)),
        "session_cache": redis_service is not None
        and getattr(session_cache, "_redis_service", None) is redis_service,
        "event_cache": redis_service is not None
        and getattr(event_cache, "_redis", None) is redis_service,
        "runtime_relay": redis_service is not None
        and getattr(runtime_relay, "redis_service", None) is redis_service,
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


@router.get("/api/livez", include_in_schema=False)
async def livez() -> dict[str, str]:
    """Cheap process liveness probe for Kubernetes.

    This endpoint intentionally avoids provider, database, executor, memory,
    and LLM checks. Kubernetes liveness should only verify that the HTTP stack
    can serve traffic; expensive dependency diagnostics belong to /api/health.
    """

    return {"status": "alive"}


@router.get("/api/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Cheap readiness probe for Kubernetes traffic routing.

    Keep this endpoint independent of optional/degraded providers so transient
    Mnemory, Intaris, executor, or LLM slowness does not remove Cognis from
    service when it can still serve traffic.
    """

    runtime = request.app.state.controller_runtime
    if runtime.state is not ControllerLifecycleState.READY:
        return JSONResponse(
            {"status": "not_ready", "reason": runtime.state.value},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not await check_connection(request.app.state.engine):
        return JSONResponse(
            {"status": "not_ready", "reason": "database_unavailable"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not runtime.schema_compatible:
        return JSONResponse(
            {"status": "not_ready", "reason": "schema_incompatible"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return JSONResponse({"status": "ready"})


@router.get("/api/health/providers")
async def health_providers(request: Request) -> dict[str, object]:
    providers = await request.app.state.providers.health()
    return {name: provider.model_dump() for name, provider in providers.items()}


@router.get("/api/v1/pwa-reset", response_class=HTMLResponse, include_in_schema=False)
async def pwa_reset() -> HTMLResponse:
    """Serve a non-SW recovery page for installed clients with stuck app shells."""

    return HTMLResponse(
        PWA_RESET_HTML,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


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
        redis=_redis_diagnostics(request),
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


@router.get(
    "/.well-known/cognis-client.json",
    response_model=ClientDiscoveryResponse,
)
async def client_discovery(request: Request, response: Response) -> ClientDiscoveryResponse:
    """Describe the stable client contract without requiring authentication."""

    response.headers["Cache-Control"] = "no-store"
    return ClientDiscoveryResponse(
        product=ClientDiscoveryProduct(),
        protocol=ClientDiscoveryProtocol(),
        server=ClientDiscoveryServer(
            id=f"cognis:{request.app.state.jwt_public_key_fingerprint}",
            version=__version__,
            build_id=__version__,
        ),
        paths=ClientDiscoveryPaths(),
        capabilities=ClientDiscoveryCapabilities(),
    )


@router.get("/.well-known/agent.json")
async def default_agent_card() -> JSONResponse:
    raise api_exception(
        501,
        "not_implemented",
        "Default public agent card requires additional discovery metadata and is deferred in MVP.",
    )
