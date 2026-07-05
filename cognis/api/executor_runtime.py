"""Helpers for websocket executor runtime reconciliation."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.tool import ExecutorCapabilities, ToolDefinition
from cognis.store.queries import (
    bump_executor_reconfigure_generation,
    get_executor_row,
    update_executor_runtime_state,
)
from cognis.tools.skills import resolve_skills_for_agent

_logger = get_logger(__name__)

CONFIGURE_RPC_TIMEOUT_SECONDS = 120.0
RUNTIME_METADATA_SCHEMA_VERSION = 1
CONFIGURE_CAPABILITY_MCP_RUNTIME_STATUS = "mcp_runtime_status_v1"
MAX_SAFE_ERROR_LENGTH = 240
MAX_SAFE_STDERR_LENGTH = 240


def schedule_executor_reconfigure(app: Any, executor_id: str) -> None:
    """Schedule a best-effort background reconcile for a websocket executor."""
    tasks: dict[str, asyncio.Task[None]] = getattr(app.state, "executor_reconcile_tasks", {})
    if not hasattr(app.state, "executor_reconcile_tasks"):
        app.state.executor_reconcile_tasks = tasks
    existing = tasks.get(executor_id)
    if existing is not None and not existing.done():
        return

    async def _run() -> None:
        try:
            await reconcile_executor(app, executor_id)
        except Exception as exc:
            _logger.warning(
                "executor_runtime: background reconcile failed for %s: %s",
                executor_id,
                _safe_error_message(str(exc)),
                exc_info=True,
            )
            try:
                await _mark_reconcile_failed(app, executor_id, exc)
            except Exception:
                _logger.warning(
                    "executor_runtime: failed to persist reconcile failure for %s",
                    executor_id,
                    exc_info=True,
                )
        finally:
            current = tasks.get(executor_id)
            if current is asyncio.current_task():
                tasks.pop(executor_id, None)

    tasks[executor_id] = asyncio.create_task(_run(), name=f"executor-reconcile-{executor_id}")


async def reconcile_executor(app: Any, executor_id: str, *, connection: Any | None = None) -> bool:
    """Reconcile a connected websocket executor to the desired generation."""
    _logger.info("executor_runtime: reconciling executor %s", executor_id)
    lock = _get_executor_lock(app, executor_id)
    async with lock:
        while True:
            async with app.state.session_factory() as session:
                row = await get_executor_row(session, executor_id)
            if row is None or row.executor_type != "websocket" or row.status != "active":
                _logger.info(
                    "executor_runtime: skipping reconcile for %s (not found, not websocket, or inactive)",
                    executor_id,
                )
                return False

            target_version = max(int(getattr(row, "desired_config_version", 0) or 0), 1)
            applied_version = int(getattr(row, "applied_config_version", 0) or 0)
            current_conn = connection or app.state.providers.executor.websocket.get_connection(
                executor_id
            )
            if current_conn is None or not getattr(current_conn, "connected", False):
                _logger.info(
                    "executor_runtime: executor %s not connected, skipping reconcile",
                    executor_id,
                )
                if getattr(row, "runtime_state", "offline") == "reconfiguring":
                    await _mark_reconcile_unavailable(app, row)
                return False

            if applied_version == target_version and getattr(row, "runtime_state", "offline") in {
                "active",
                "degraded",
            }:
                observed_tools = list(getattr(row, "observed_tools", None) or [])
                runtime_metadata = getattr(row, "runtime_metadata", None) or {}
                app.state.providers.executor.websocket.mark_ready(
                    executor_id,
                    ExecutorCapabilities(
                        tools=[
                            str(tool.get("name", "")) for tool in observed_tools if tool.get("name")
                        ],
                        inference=True,
                        inference_models=[],
                        inference_type="litellm_proxy",
                        channels=True,
                    ),
                    metadata=_executor_connection_metadata(
                        labels=row.labels or {},
                        environment=runtime_metadata.get("environment"),
                        platform=runtime_metadata.get("platform") or {},
                        status=row.status,
                        runtime_metadata=runtime_metadata,
                    ),
                )
                _logger.info(
                    "executor_runtime: executor %s already at desired v%d (%s), no reconfigure needed",
                    executor_id,
                    target_version,
                    getattr(row, "runtime_state", "?"),
                )
                return True

            _logger.info(
                "executor_runtime: executor %s needs reconfigure: desired v%d, applied v%d",
                executor_id,
                target_version,
                applied_version,
            )
            async with app.state.session_factory() as session:
                await update_executor_runtime_state(
                    session,
                    executor_id,
                    desired_config_version=target_version,
                    runtime_state="reconfiguring",
                )
                await session.commit()

            configure_metadata: dict[str, Any] = getattr(row, "runtime_metadata", None) or {}
            try:
                _logger.info(
                    "executor_runtime: building configure payload for %s v%d",
                    executor_id,
                    target_version,
                )
                payload, configure_metadata = await _build_configure_payload(
                    app, row, target_version
                )
                _logger.info(
                    "executor_runtime: sending executor.configure RPC to %s (timeout=%ds, %d MCP server(s))",
                    executor_id,
                    CONFIGURE_RPC_TIMEOUT_SECONDS,
                    len(payload.get("mcp_servers", [])),
                )
                configure_result = await current_conn.rpc_call(
                    "executor.configure",
                    payload,
                    timeout=CONFIGURE_RPC_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                _logger.warning(
                    "executor_runtime: executor %s configure failed: %s",
                    executor_id,
                    _safe_error_message(str(exc)),
                )
                if not _is_current_connection(app, executor_id, current_conn):
                    _logger.info(
                        "executor_runtime: executor %s connection replaced during configure, retrying",
                        executor_id,
                    )
                    connection = None
                    continue
                runtime_metadata = _merge_runtime_metadata(
                    configure_metadata,
                    {
                        "runtime_state": "blocked",
                        "warnings": [
                            f"Executor reconfigure failed: {_safe_error_message(str(exc))}"
                        ],
                    },
                )
                await _persist_runtime_state(
                    app,
                    executor_id,
                    runtime_state="blocked",
                    applied_config_version=int(getattr(row, "applied_config_version", 0) or 0),
                    observed_tools=list(getattr(row, "observed_tools", None) or []),
                    runtime_metadata=runtime_metadata,
                )
                return False

            if not _is_current_connection(app, executor_id, current_conn):
                _logger.info(
                    "executor_runtime: executor %s connection replaced after configure, retrying",
                    executor_id,
                )
                connection = None
                continue

            caps_raw = configure_result.get("capabilities") or {}
            runtime_state = str(configure_result.get("runtime_state") or "active")
            applied_version = int(configure_result.get("applied_version") or target_version)
            observed_tools = list(configure_result.get("observed_tools") or [])
            _logger.info(
                "executor_runtime: executor %s configure result: state=%s, applied v%d, %d tool(s)",
                executor_id,
                runtime_state,
                applied_version,
                len(observed_tools),
            )
            runtime_metadata = _merge_runtime_metadata(
                configure_metadata,
                dict(configure_result.get("runtime_metadata") or {}),
            )
            capabilities = ExecutorCapabilities(
                tools=list(caps_raw.get("tools") or []),
                inference=bool(caps_raw.get("inference", False)),
                inference_models=list(caps_raw.get("inference_models") or []),
                inference_type=caps_raw.get("inference_type"),
                channels=bool(caps_raw.get("channels", False)),
            )
            app.state.providers.executor.websocket.mark_ready(
                executor_id,
                capabilities,
                metadata=_executor_connection_metadata(
                    labels=row.labels or {},
                    environment=runtime_metadata.get("environment"),
                    platform=runtime_metadata.get("platform") or {},
                    status=row.status,
                    runtime_metadata=runtime_metadata,
                ),
            )
            await _persist_runtime_state(
                app,
                executor_id,
                runtime_state=runtime_state,
                applied_config_version=applied_version,
                observed_tools=observed_tools,
                runtime_metadata=runtime_metadata,
            )
            await _invalidate_mcp_oauth_tokens_for_runtime_failures(
                app,
                row,
                runtime_metadata=runtime_metadata,
            )

            async with app.state.session_factory() as session:
                refreshed = await get_executor_row(session, executor_id)
            if refreshed is None:
                return False
            if int(getattr(refreshed, "desired_config_version", 0) or 0) > applied_version:
                _logger.info(
                    "executor_runtime: executor %s has newer desired version after apply, re-reconciling",
                    executor_id,
                )
                connection = None
                continue
            _logger.info(
                "executor_runtime: executor %s reconciled to v%d (%s)",
                executor_id,
                applied_version,
                runtime_state,
            )
            return True


async def _persist_runtime_state(
    app: Any,
    executor_id: str,
    *,
    runtime_state: str,
    applied_config_version: int,
    observed_tools: list[dict[str, Any]],
    runtime_metadata: dict[str, Any],
) -> None:
    async with app.state.session_factory() as session:
        row = await update_executor_runtime_state(
            session,
            executor_id,
            applied_config_version=applied_config_version,
            observed_tools=observed_tools,
            runtime_metadata=runtime_metadata,
            last_observed_at=datetime.now(UTC),
            runtime_state=runtime_state,
        )
        await session.commit()
    queue = getattr(app.state, "tool_classification_queue", None)
    if queue is None or row is None or not observed_tools:
        return
    try:
        tool_defs = [
            ToolDefinition.model_validate(item) for item in observed_tools if isinstance(item, dict)
        ]
        await queue.enqueue_tools(tool_defs, owner_email=getattr(row, "owner_email", None))
    except Exception:
        _logger.warning(
            "executor_runtime: failed to enqueue tool classifications",
            extra={
                "extra_data": {"executor_id": executor_id, "observed_tools": len(observed_tools)}
            },
            exc_info=True,
        )


async def _invalidate_mcp_oauth_tokens_for_runtime_failures(
    app: Any,
    row: Any,
    *,
    runtime_metadata: dict[str, Any],
) -> None:
    """Invalidate OAuth tokens rejected by MCP resource servers and request one retry."""

    service = getattr(app.state.providers, "mcp_oauth_service", None)
    if service is None:
        return
    failed_server_ids = _authorization_failed_mcp_server_ids(runtime_metadata)
    if not failed_server_ids:
        return

    invalidated = False
    for server_id in failed_server_ids:
        try:
            marked = await service.mark_token_invalid_for_server(
                user_email=str(getattr(row, "owner_email", "") or ""),
                server_id=server_id,
                reason="mcp_resource_authorization_failed",
            )
        except Exception:
            _logger.warning(
                "executor_runtime: failed to invalidate rejected MCP OAuth token",
                extra={
                    "extra_data": {
                        "executor_id": row.executor_id,
                        "server_id": server_id,
                    }
                },
                exc_info=True,
            )
            continue
        invalidated = invalidated or bool(marked)

    if not invalidated:
        return

    async with app.state.session_factory() as session:
        bumped = await bump_executor_reconfigure_generation(
            session,
            row.executor_id,
            runtime_state="reconfiguring",
        )
        await session.commit()
    if not bumped:
        return
    _logger.info(
        "executor_runtime: scheduled executor reconfigure after MCP OAuth token invalidation",
        extra={
            "extra_data": {
                "executor_id": row.executor_id,
                "server_ids": failed_server_ids,
            }
        },
    )


def _authorization_failed_mcp_server_ids(runtime_metadata: dict[str, Any]) -> list[str]:
    servers = runtime_metadata.get("mcp_servers")
    if not isinstance(servers, list):
        return []
    server_ids: list[str] = []
    seen: set[str] = set()
    for item in servers:
        if not isinstance(item, dict):
            continue
        server_id = item.get("server_id")
        if not isinstance(server_id, str) or not server_id or server_id in seen:
            continue
        if item.get("authorization_required") is not True:
            continue
        if item.get("status") != "failed":
            continue
        server_ids.append(server_id)
        seen.add(server_id)
    return server_ids


async def _mark_reconcile_unavailable(app: Any, row: Any) -> None:
    """Move an unavailable executor out of transient reconfiguring state."""
    runtime_metadata = _merge_runtime_metadata(
        getattr(row, "runtime_metadata", None) or {},
        {
            "runtime_state": "stale",
            "warnings": [
                "Executor reconfigure could not continue because the connection is unavailable."
            ],
        },
    )
    await _persist_runtime_state(
        app,
        row.executor_id,
        runtime_state="stale",
        applied_config_version=int(getattr(row, "applied_config_version", 0) or 0),
        observed_tools=list(getattr(row, "observed_tools", None) or []),
        runtime_metadata=runtime_metadata,
    )


async def _mark_reconcile_failed(app: Any, executor_id: str, exc: Exception) -> None:
    """Persist a terminal reconcile failure so schedulers do not see a transient state forever."""
    async with app.state.session_factory() as session:
        row = await get_executor_row(session, executor_id)
    if row is None or getattr(row, "runtime_state", "offline") != "reconfiguring":
        return
    runtime_metadata = _merge_runtime_metadata(
        getattr(row, "runtime_metadata", None) or {},
        {
            "runtime_state": "blocked",
            "warnings": [
                f"Executor background reconfigure failed: {_safe_error_message(str(exc))}"
            ],
        },
    )
    await _persist_runtime_state(
        app,
        executor_id,
        runtime_state="blocked",
        applied_config_version=int(getattr(row, "applied_config_version", 0) or 0),
        observed_tools=list(getattr(row, "observed_tools", None) or []),
        runtime_metadata=runtime_metadata,
    )


async def _build_configure_payload(
    app: Any,
    row: Any,
    desired_version: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from cognis.api.executor_ws import _resolve_executor_mcp_payload
    from cognis.api.runtime_support import _resolve_web_config
    from cognis.tools.skills import _qualified_skill_tool_name

    mcp_servers, scoped_secrets, mcp_metadata = await _resolve_executor_mcp_payload(
        row, app.state.providers
    )
    web_config = await _resolve_web_config(app.state.providers, row.owner_email)
    scoped_secrets.update(web_config.get("web_secrets", {}))
    skill_manifests: list[dict[str, Any]] = []
    try:
        dummy_agent = AgentDefinition(
            agent_id="executor_configure",
            owner_email=row.owner_email,
            name="executor_configure",
        )
        async with app.state.session_factory() as db_session:
            resolved = await resolve_skills_for_agent(
                db_session, dummy_agent, owner_email=row.owner_email
            )
        for skill in resolved.skills:
            if skill.tools or skill.asset_manifest:
                manifest: dict[str, Any] = {
                    "skill_id": skill.skill_id,
                    "version_id": skill.version_id,
                    "content_hash": skill.content_hash,
                    "tools": [
                        {
                            **t.model_dump(mode="json"),
                            "qualified_name": _qualified_skill_tool_name(skill.skill_id, t.name),
                        }
                        for t in skill.tools
                    ],
                    "asset_manifest": [a.model_dump(mode="json") for a in skill.asset_manifest],
                }
                artifact_store = getattr(app.state, "artifact_store", None)
                if artifact_store and skill.asset_manifest:
                    ttl_seconds = getattr(
                        getattr(artifact_store, "_config", None),  # noqa: SLF001
                        "signed_url_ttl_seconds",
                        None,
                    )
                    for asset_entry in manifest["asset_manifest"]:
                        ns = asset_entry.get("artifact_namespace", "skills")
                        oid = asset_entry.get("artifact_object_id", "")
                        filename = asset_entry.get("filename", "")
                        if oid:
                            with contextlib.suppress(Exception):
                                asset_entry["url"] = await artifact_store.async_get_public_url(
                                    ns,
                                    oid,
                                    filename,
                                    ttl_seconds=ttl_seconds,
                                )
                skill_manifests.append(manifest)
    except Exception:
        _logger.warning(
            "executor_runtime: failed to resolve skill manifests",
            extra={"extra_data": {"executor_id": row.executor_id}},
            exc_info=True,
        )

    metadata = {
        "schema_version": RUNTIME_METADATA_SCHEMA_VERSION,
        "configure_capabilities": [CONFIGURE_CAPABILITY_MCP_RUNTIME_STATUS],
        "single_controller_process": True,
        "legacy_metadata": False,
        "mcp_servers": [],
        "warnings": [],
        "config_version": desired_version,
        "platform": {},
        "environment": {},
    }
    if mcp_metadata:
        metadata["mcp_servers"] = list(mcp_metadata.get("mcp_servers") or [])
        metadata["warnings"] = list(mcp_metadata.get("warnings") or [])
    payload = {
        "config_version": desired_version,
        "enabled_tools": row.enabled_tools or [],
        "enabled_tool_groups": row.enabled_tool_groups or [],
        "config": row.config or {},
        "mcp_servers": [server.model_dump(mode="json") for server in mcp_servers],
        "secrets": scoped_secrets,
        "web_config": {
            "web_backend": web_config.get("web_backend", "direct"),
            "web_search_backend": web_config.get("web_search_backend", "direct"),
            "web_fetch_backend": web_config.get("web_fetch_backend", "direct"),
            "web_fetch_fallback_browser": web_config.get("web_fetch_fallback_browser", True),
            "web_searxng_url": web_config.get("web_searxng_url", ""),
            "web_searxng_engines": web_config.get("web_searxng_engines", ""),
            "web_searxng_categories": web_config.get("web_searxng_categories", ""),
            "web_searxng_language": web_config.get("web_searxng_language", ""),
            "web_browser_fetch_session_idle_seconds": web_config.get(
                "web_browser_fetch_session_idle_seconds", 60
            ),
            "web_browser_fetch_wait_timeout_seconds": web_config.get(
                "web_browser_fetch_wait_timeout_seconds", 30
            ),
            "web_browser_fetch_navigation_timeout_seconds": web_config.get(
                "web_browser_fetch_navigation_timeout_seconds", 60
            ),
            "web_browser_fetch_wait_until": web_config.get(
                "web_browser_fetch_wait_until", "domcontentloaded"
            ),
            "web_browser_fetch_network_idle_after_dom_seconds": web_config.get(
                "web_browser_fetch_network_idle_after_dom_seconds", 3
            ),
            "web_browser_fetch_headed_fallback_enabled": web_config.get(
                "web_browser_fetch_headed_fallback_enabled", False
            ),
            "web_concurrency": web_config.get("web_concurrency", {}),
            "web_available_backends": web_config.get("web_available_backends", ["direct"]),
            "web_available_search_backends": web_config.get(
                "web_available_search_backends", ["direct"]
            ),
            "web_available_fetch_backends": web_config.get(
                "web_available_fetch_backends", ["direct"]
            ),
        },
        "skill_manifests": skill_manifests,
    }
    return payload, metadata


def _merge_runtime_metadata(base: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged.update(result)
    merged.setdefault("schema_version", RUNTIME_METADATA_SCHEMA_VERSION)
    merged.setdefault("configure_capabilities", [CONFIGURE_CAPABILITY_MCP_RUNTIME_STATUS])
    merged.setdefault("legacy_metadata", False)
    merged.setdefault("mcp_servers", [])
    merged.setdefault("warnings", [])
    warnings = merged.get("warnings")
    if isinstance(warnings, list):
        base_warnings = base.get("warnings") if isinstance(base.get("warnings"), list) else []
        result_warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        merged["warnings"] = [
            str(item)[:MAX_SAFE_ERROR_LENGTH] for item in [*base_warnings, *result_warnings]
        ][:10]
    else:
        merged["warnings"] = []
    base_mcp_servers = base.get("mcp_servers") if isinstance(base.get("mcp_servers"), list) else []
    result_mcp_servers = (
        result.get("mcp_servers") if isinstance(result.get("mcp_servers"), list) else []
    )
    merged["mcp_servers"] = [*base_mcp_servers, *result_mcp_servers]
    return merged


def _safe_error_message(message: str) -> str:
    return " ".join(message.split())[:MAX_SAFE_ERROR_LENGTH] or "unknown error"


def _get_executor_lock(app: Any, executor_id: str) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = getattr(app.state, "executor_reconcile_locks", {})
    if not hasattr(app.state, "executor_reconcile_locks"):
        app.state.executor_reconcile_locks = locks
    lock = locks.get(executor_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[executor_id] = lock
    return lock


def _is_current_connection(app: Any, executor_id: str, connection: Any) -> bool:
    return app.state.providers.executor.websocket.get_connection(executor_id) is connection


def _executor_connection_metadata(
    *,
    labels: dict[str, Any],
    environment: Any,
    platform: dict[str, Any],
    status: str,
    runtime_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "labels": labels,
        "platform": platform,
        "status": status,
    }
    if isinstance(environment, dict):
        metadata["environment"] = environment
    if runtime_metadata is not None:
        metadata["runtime_metadata"] = runtime_metadata
    return metadata
